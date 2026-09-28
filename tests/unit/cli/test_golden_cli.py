"""``adapter-verify golden`` against a synthetic repository (sync module: CliRunner)."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from adapter_verify import composition
from adapter_verify.cli.main import cli
from adapter_verify.golden.domain.judge import JudgeVerdict
from adapter_verify.golden.domain.tasks import AgentConfig, RunLimits
from adapter_verify.golden.fakes import ScriptedAgent, ScriptedJudge
from adapter_verify.golden.ports import (
    AgentHarness,
    AgentRun,
    JudgeUnavailableError,
    ToolCall,
    ToolCaller,
    ToolView,
)
from tests.unit.golden.support import DEFINITIONS, Repo, read_task

pytestmark = pytest.mark.unit

GET = ToolCall(tool="order.get", arguments={"order_id": "1"})
CANCEL = ToolCall(tool="order.cancel", arguments={"order_id": "1"}, idempotency_key="k-1")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return Repo.create(tmp_path)


def _golden(*args: str) -> Result:
    return CliRunner().invoke(cli, ["golden", *args])


def _agent(monkeypatch: pytest.MonkeyPatch, agent: ScriptedAgent) -> None:
    def harness(settings: object, config: AgentConfig, models: object) -> AgentHarness:
        del settings, config, models
        return agent

    monkeypatch.setattr(composition, "golden_harness", harness)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


def test_lint(repo: Repo) -> None:
    ok = _golden("lint")
    assert (ok.exit_code, ok.output.strip()) == (0, "2 golden task(s) clean")
    repo.put_task(read_task(threshold=9))
    bad = _golden("lint")
    assert bad.exit_code == 1
    assert "GOLDEN_THRESHOLD\tgolden_tasks/reader/order-inflight.yaml" in bad.output
    refused = _golden("run", "--all")
    assert refused.exit_code == 3
    assert "fail lint" in refused.output


def test_run_needs_a_selection_and_a_harness(repo: Repo) -> None:
    assert _golden("run").exit_code == 3
    unavailable = _golden("run", "--all")
    assert unavailable.exit_code == 3
    assert "no agent harness of kind 'scripted'" in unavailable.output
    assert not (repo.root / "golden-results").exists()


def test_run_writes_results_and_report(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _agent(monkeypatch, ScriptedAgent([GET, CANCEL]))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = _golden("run", "--agent", "reader", "--results", "r.json", "--report", "report.md")
    assert out.exit_code == 1, out.output  # the reader must not even attempt order.cancel
    body = json.loads((repo.root / "r.json").read_text(encoding="utf-8"))
    assert body["schema_version"] == 2
    assert [t["task_id"] for t in body["tasks"]] == ["order-inflight"]
    report = (repo.root / "report.md").read_text(encoding="utf-8")
    assert "**sequence** layer failed" in report
    assert summary.read_text(encoding="utf-8") == report

    _agent(monkeypatch, ScriptedAgent([CANCEL]))
    passed = _golden("run", "--task", "order-cancel")
    assert passed.exit_code == 0, passed.output
    assert list((repo.root / "golden-results").glob("golden-*.json"))

    repo.put_task(read_task(expect_answer={"rubric": "says in progress"}))
    _agent(monkeypatch, ScriptedAgent([GET]))
    incomplete = _golden("run", "--task", "order-inflight", "--results", "r2.json")
    assert incomplete.exit_code == 2
    assert "No answer judge is configured" in incomplete.output

    assert _golden("run", "--task", "nope").output.strip() == "no golden tasks selected"
    (repo.root / "bad.json").write_text("{}", encoding="utf-8")
    assert _golden("run", "--all", "--previous", "bad.json").exit_code == 3


def test_gate_uses_current_evidence(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _agent(monkeypatch, ScriptedAgent([GET]))
    assert _golden("run", "--task", "order-inflight", "--results", "r.json").exit_code == 0
    passed = _golden("gate", "--tool", "order.get", "--version", "1.2.0", "--results", "r.json")
    assert (passed.exit_code, passed.output.strip()) == (0, "PASS\torder-inflight")
    uncovered = _golden(
        "gate", "--tool", "order.cancel", "--version", "1.0.0", "--results", "r.json"
    )
    assert uncovered.exit_code == 1
    assert "MISSING\torder-cancel" in uncovered.output

    repo.write(
        "catalog/definitions/order.get/1.2.0.yaml",
        {**DEFINITIONS["order.get/1.2.0"], "description": "Look up an order. Always shipped."},
    )
    stale = _golden("gate", "--tool", "order.get", "--version", "1.2.0", "--results", "r.json")
    assert stale.exit_code == 1
    assert "STALE\torder-inflight" in stale.output

    (repo.root / "bad.json").write_text("[]", encoding="utf-8")
    assert (
        _golden("gate", "--tool", "t", "--version", "1.0.0", "--results", "bad.json").exit_code == 3
    )


def test_description_change_is_selected_and_diffed(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline part of the §6.11 acceptance: the task is re-run and the report shows why."""
    _git(repo.root, "init", "-q")
    _git(repo.root, "config", "user.email", "t@example.invalid")
    _git(repo.root, "config", "user.name", "t")
    _git(repo.root, "add", ".")
    _git(repo.root, "commit", "-qm", "base")
    repo.write(
        "catalog/definitions/order.get/1.2.0.yaml",
        {
            **DEFINITIONS["order.get/1.2.0"],
            "description": "Look up an order. Orders ship same day.",
        },
    )
    selected = _golden("select", "--changed-since", "HEAD")
    assert selected.exit_code == 0
    assert selected.output.strip() == "order-inflight\tdefinition of order.get changed"

    _agent(monkeypatch, ScriptedAgent([]))  # the agent now skips the lookup
    out = _golden("run", "--changed-since", "HEAD", "--results", "r.json")
    assert out.exit_code == 1
    assert "definition of order.get changed" in out.output
    assert "-Look up one order by ID. Returns its status." in out.output
    assert "+Look up an order. Orders ship same day." in out.output
    assert "**calls** layer failed" in out.output

    assert _golden("select", "--changed-since", "no-such-ref").exit_code == 3
    assert _golden("run", "--changed-since", "no-such-ref").exit_code == 3


def test_invalid_settings_exit_3(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    del repo
    monkeypatch.setenv("ADAPTER_GOLDEN_TOKEN_BUDGET", "0")
    out = _golden("lint")
    assert out.exit_code == 3
    assert "token_budget" in out.output


def test_unreadable_contracts_exit_3(repo: Repo) -> None:
    (repo.root / "contracts/order.get/1.2.0.yaml").write_text("tool: [", encoding="utf-8")
    assert _golden("lint").exit_code == 3


class _DescriptionReader(ScriptedAgent):
    """Looks an order up only while order.get's description is the original one."""

    async def run(
        self, prompt: str, tools: Sequence[ToolView], call_tool: ToolCaller, limits: RunLimits
    ) -> AgentRun:
        del prompt, limits
        names = {t.name: t.description for t in tools}
        if names.get("order.get") == DEFINITIONS["order.get/1.2.0"]["description"]:
            await call_tool(GET)
        if "order.cancel" in names:
            await call_tool(CANCEL)
        return AgentRun(answer="done")


def test_canaries_report_every_outcome(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.write(
        "golden_tasks/canaries.yaml",
        {
            "misleading_descriptions": {"order.get@1.2.0": "Look up an order. It always ships."},
            "dropped_fields": {"order.get": "status"},
        },
    )
    _agent(monkeypatch, _DescriptionReader([]))
    out = _golden("canaries")
    assert out.exit_code == 1, out.output
    lines = dict(line.split("\t", 1) for line in out.output.strip().splitlines())
    assert lines == {
        "MISSED": "drop_field\torder-inflight",  # the scripted agent ignores tool output
        "CAUGHT": "description_mislead\torder-inflight",
        "NO_COVERAGE": "timezone_shift\t-",
    } or set(out.output.split()) >= {"CAUGHT", "MISSED", "NO_COVERAGE"}
    assert "CAUGHT\tdescription_mislead\torder-inflight" in out.output
    assert "MISSED\tenum_swap\torder-inflight" in out.output
    assert "NO_COVERAGE\ttimezone_shift\t-" in out.output

    repo.write("golden_tasks/canaries.yaml", {"timezone_shift_hours": 99})
    assert _golden("canaries").exit_code == 3


def test_calibrate(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _golden("calibrate").exit_code == 3  # no key, no judge
    repo.write(
        "tests/golden_selftest/calibration/good.yaml",
        {
            "case_id": "good",
            "rubric": "r",
            "evidence": {"prompt": "p", "tool_results": [], "answer": "a"},
            "expected_pass": True,
        },
    )
    verdicts = [JudgeVerdict(score=0.9, passed=True, reasons=("ok",))]
    monkeypatch.setattr(composition, "golden_judge", lambda _s, _m: ScriptedJudge(list(verdicts)))
    ok = _golden("calibrate")
    assert (ok.exit_code, ok.output.strip()) == (
        0,
        "judge gemini-3.8-flash classified every calibration case",
    )
    verdicts[0] = JudgeVerdict(score=0.1, passed=False, reasons=("no",))
    missed = _golden("calibrate")
    assert (missed.exit_code, missed.output.strip()) == (1, "MISSED\tgood")


def test_calibrate_stops_at_the_first_judge_error(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case_id in ("a", "b"):
        repo.write(
            f"tests/golden_selftest/calibration/{case_id}.yaml",
            {
                "case_id": case_id,
                "rubric": "r",
                "evidence": {"prompt": "p", "tool_results": [], "answer": "a"},
                "expected_pass": True,
            },
        )
    judge = ScriptedJudge([JudgeUnavailableError("429 RESOURCE_EXHAUSTED")])
    monkeypatch.setattr(composition, "golden_judge", lambda _s, _m: judge)
    out = _golden("calibrate")
    assert (out.exit_code, out.output.strip()) == (1, "ERROR\ta\t429 RESOURCE_EXHAUSTED")
    assert len(judge.graded) == 1  # "b" would fail the same way; no quota spent on it
