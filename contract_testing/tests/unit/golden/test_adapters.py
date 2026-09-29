import gc
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from adapter_verify import composition
from adapter_verify.common.adapters.yamlfile import ConfigFileError
from adapter_verify.golden.adapters.egress import SocketEgressGuard
from adapter_verify.golden.adapters.files import (
    load_calibration,
    load_workspace,
    read_results,
    write_results,
)
from adapter_verify.golden.adapters.gemini import ClientModels, GeminiAgent, GeminiJudge
from adapter_verify.golden.adapters.git import GitError, GitRepo
from adapter_verify.golden.adapters.nvidia import NvidiaAgent, NvidiaJudge
from adapter_verify.golden.domain.results import SuiteResults, TokenUsage
from adapter_verify.golden.domain.tasks import AgentConfig, ModelProvider
from adapter_verify.golden.ports import HarnessUnavailableError
from adapter_verify.settings import GoldenSettings
from tests.unit.golden.support import Repo, read_task

pytestmark = pytest.mark.unit


def test_workspace_loads_and_tolerates_bad_fixtures(tmp_path: Path) -> None:
    repo = Repo.create(tmp_path)
    repo.put_task(
        read_task(
            fixtures=[
                {"tool": "order.get", "payload": "fixtures/golden/order_1_inprog.synthetic.json"},
                {"tool": "order.get", "payload": "fixtures/golden/order_1_inprog.synthetic.json"},
                {"tool": "order.get", "payload": "../outside.synthetic.json"},
                {"tool": "order.get", "payload": "fixtures/golden/bad.synthetic.json"},
            ]
        )
    )
    (tmp_path / "fixtures/golden/bad.synthetic.json").write_text("{not json", encoding="utf-8")
    (tmp_path.parent / "outside.synthetic.json").write_text("{}", encoding="utf-8")
    ws = load_workspace(tmp_path, Path("golden_tasks"), Path("catalog/definitions"))
    assert set(ws.inputs.fixtures) == {
        "fixtures/golden/order_1_inprog.synthetic.json",
        "fixtures/golden/order_1_cancel.synthetic.json",  # the write task's
    }
    assert "fixtures/golden/bad.synthetic.json" in ws.fixture_digests  # hashed, not parsed
    assert "../outside.synthetic.json" not in ws.fixture_digests  # never leaves the repo
    assert ws.fixture("fixtures/golden/missing.synthetic.json") is None
    assert ws.quarantine.entries == ()
    assert sorted(ws.agents) == ["reader", "writer"]


def test_calibration_loader_rejects_invalid_cases(tmp_path: Path) -> None:
    assert load_calibration(tmp_path) == []
    (tmp_path / "good.yaml").write_text(
        "case_id: good\nrubric: r\nexpected_pass: true\n"
        "evidence: {prompt: p, tool_results: [], answer: a}\n",
        encoding="utf-8",
    )
    assert [c.case_id for c in load_calibration(tmp_path)] == ["good"]
    (tmp_path / "bad.yaml").write_text("case_id: bad\n", encoding="utf-8")
    with pytest.raises(ConfigFileError, match=r"bad\.yaml"):
        load_calibration(tmp_path)


def test_results_round_trip(tmp_path: Path) -> None:
    at = datetime(2026, 9, 26, tzinfo=UTC)
    results = SuiteResults(
        run_id="r",
        git_sha="abc",
        started_at=at,
        finished_at=at,
        agents=(),
        judge_model=None,
        judge_calibrated=None,
        token_budget=None,
        usage=TokenUsage(),
        budget_exceeded=False,
        tasks=(),
    )
    path = tmp_path / "out/r.json"
    write_results(results, path)
    assert read_results(path) == results
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigFileError, match="not a readable results file"):
        read_results(path)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


def test_git_changes_show_and_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first")
    repo = GitRepo(tmp_path)
    assert repo.head() is not None
    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("new\n", encoding="utf-8")
    assert repo.changed_paths("HEAD") == ["a.txt", "b.txt"]
    assert repo.show("HEAD", "a.txt") == "one\n"
    assert repo.show("HEAD", "b.txt") is None
    with pytest.raises(GitError):
        repo.changed_paths("no-such-ref")


def test_git_outside_a_repository(tmp_path: Path) -> None:
    assert GitRepo(tmp_path / "missing").head() is None


def test_egress_guard_blocks_and_restores() -> None:
    real = socket.getaddrinfo
    guard = SocketEgressGuard(frozenset({"allowed.invalid"}))
    with guard.guard() as blocked:
        with pytest.raises(OSError, match="egress blocked"):
            socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(OSError, match="egress blocked"):
            socket.create_connection(("192.0.2.1", 443), timeout=0.1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError, match="egress blocked"):
                s.connect_ex(("192.0.2.1", 443))
        finally:
            s.close()
        with pytest.raises(socket.gaierror):  # allowed by the guard; resolution itself fails
            socket.getaddrinfo("allowed.invalid", 443)
    assert blocked == ["example.invalid", "192.0.2.1", "192.0.2.1"]
    assert socket.getaddrinfo is real


def test_egress_guard_lets_loopback_self_pipes_through() -> None:
    with SocketEgressGuard().guard() as blocked:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen()
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(server.getsockname())
        finally:
            client.close()
            server.close()
    assert blocked == []


def test_guard_allows_addresses_resolved_for_allowed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: object, *args: object, **kwargs: object) -> list[object]:
        del host, args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with SocketEgressGuard(frozenset({"api.allowed.invalid"})).guard() as blocked:
        socket.getaddrinfo("api.allowed.invalid", 443)
        socket.getaddrinfo(b"api.allowed.invalid", 443)  # anyio passes IDNA-encoded bytes
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.01)
        try:
            s.connect_ex(("192.0.2.9", 443))  # allowed: resolved from an allowed host
        finally:
            s.close()
    assert blocked == []


def test_gemini_wiring_needs_a_key_and_a_known_harness() -> None:
    reference = AgentConfig(agent_id="a", harness="reference", model="m", system_prompt="s")
    no_key = GoldenSettings(judge_provider="gemini")
    assert composition.gemini_models(no_key) is None
    assert composition.golden_judge(no_key, composition.ModelClients()) is None
    with pytest.raises(HarnessUnavailableError, match="ADAPTER_GOLDEN_GEMINI_API_KEY"):
        composition.golden_harness(no_key, reference, composition.ModelClients())
    keyed = GoldenSettings(gemini_api_key=SecretStr("test-key-not-real"), judge_provider="gemini")
    clients = composition.model_clients(keyed)
    models = clients.gemini
    gc.collect()  # a collected Client would close the HTTP pool under the adapters
    assert isinstance(models, ClientModels)
    pool = models._client._api_client._async_httpx_client
    assert pool is not None
    assert not pool.is_closed
    assert clients.nvidia is None
    assert isinstance(composition.golden_harness(keyed, reference, clients), GeminiAgent)
    judge = composition.golden_judge(keyed, clients)
    assert isinstance(judge, GeminiJudge)
    assert judge.model == "gemini-3.8-flash"
    with pytest.raises(HarnessUnavailableError, match="'custom'"):
        composition.golden_harness(
            keyed, reference.model_copy(update={"harness": "custom"}), clients
        )


def test_nvidia_wiring_follows_the_agent_config_and_judge_provider() -> None:
    reference = AgentConfig(
        agent_id="a",
        harness="reference",
        provider=ModelProvider.NVIDIA,
        model="m",
        system_prompt="s",
    )
    no_key = GoldenSettings()
    assert composition.golden_judge(no_key, composition.ModelClients()) is None
    with pytest.raises(HarnessUnavailableError, match="ADAPTER_GOLDEN_NVIDIA_API_KEY"):
        composition.golden_harness(no_key, reference, composition.ModelClients())
    keyed = GoldenSettings(nvidia_api_key=SecretStr("test-key-not-real"))
    clients = composition.model_clients(keyed)
    assert clients.gemini is None
    assert isinstance(composition.golden_harness(keyed, reference, clients), NvidiaAgent)
    judge = composition.golden_judge(keyed, clients)
    assert isinstance(judge, NvidiaJudge)
    assert judge.model == "moonshotai/kimi-k2.6"
    pinned = keyed.model_copy(update={"judge_model": "other/judge"})
    assert composition.golden_judge(pinned, clients).model == "other/judge"  # type: ignore[union-attr]


def test_egress_allows_the_configured_model_apis_only() -> None:
    assert composition.golden_egress_hosts(GoldenSettings()) == frozenset()
    both = GoldenSettings(
        gemini_api_key=SecretStr("test-key-not-real"),
        nvidia_api_key=SecretStr("test-key-not-real"),
        egress_allowed_hosts=("extra.invalid",),
    )
    assert composition.golden_egress_hosts(both) == {
        "extra.invalid",
        "generativelanguage.googleapis.com",
        "integrate.api.nvidia.com",
    }


def test_reference_agent_config_needs_model_and_prompt() -> None:
    with pytest.raises(ValidationError, match="model and system_prompt"):
        AgentConfig(agent_id="a", harness="reference", model="m")
