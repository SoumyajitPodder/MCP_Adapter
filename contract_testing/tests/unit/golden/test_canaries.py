from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from adapter_verify import composition
from adapter_verify.golden.domain.assertions import Layer, LayerFailure
from adapter_verify.golden.domain.canaries import (
    CanaryConfig,
    CanaryKind,
    CanaryStatus,
    affected,
    mutate,
    status,
)
from adapter_verify.golden.domain.results import (
    ErrorKind,
    RunOutcome,
    RunRecord,
    SuiteResults,
    TaskResult,
    TaskVerdict,
    TokenUsage,
)
from adapter_verify.settings import ContractSettings
from tests.unit.golden.support import Repo

pytestmark = pytest.mark.unit

FIXTURE = "fixtures/golden/order_1_inprog.synthetic.json"


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    repo = Repo.create(tmp_path)
    contract = {
        "tool": "order.get",
        "version": "1.2.0",
        "fields": [
            {"name": "order_id", "type": "string"},
            {"name": "status", "type": "enum", "values": ["in_progress", "completed", "cancelled"]},
            {"name": "created_at", "type": "datetime"},
        ],
    }
    repo.write("contracts/order.get/1.2.0.yaml", contract)
    repo.write(
        FIXTURE, {"order_id": "1", "status": "in_progress", "created_at": "2026-09-20T09:15:00Z"}
    )
    return repo


def _mutate(
    repo: Repo, kind: CanaryKind, config: CanaryConfig | None = None
) -> tuple[dict[str, JsonValue], frozenset[str], list[str]]:
    ws = repo.workspace()
    _, access = repo.settings()
    _, catalog = composition.load_access_config(access)
    contracts = composition.contracts_by_tool(
        catalog, composition.canonical_contracts(ContractSettings(root=repo.root))
    )
    m = mutate(kind, config or CanaryConfig(), ws.inputs, contracts)
    body = m.inputs.fixtures[FIXTURE]
    assert isinstance(body, dict)
    return body, m.tools, affected(ws.tasks, m.tools)


def test_enum_swap_and_timezone_shift(repo: Repo) -> None:
    body, tools, ids = _mutate(repo, CanaryKind.ENUM_SWAP)
    assert body["status"] == "completed"
    assert tools == {"order.get"}
    assert ids == ["order-inflight"]
    body, _, _ = _mutate(repo, CanaryKind.TIMEZONE_SHIFT)
    assert body["created_at"] == "2026-09-19T23:15:00Z"
    assert repo.workspace().inputs.fixtures[FIXTURE]["created_at"] == "2026-09-20T09:15:00Z"  # type: ignore[index, call-overload]


def test_drop_field_and_mislead(repo: Repo) -> None:
    body, tools, _ = _mutate(
        repo, CanaryKind.DROP_FIELD, CanaryConfig(dropped_fields={"order.get": "status"})
    )
    assert "status" not in body
    assert tools == {"order.get"}
    _, tools, ids = _mutate(repo, CanaryKind.DROP_FIELD)
    assert (tools, ids) == (frozenset(), [])
    config = CanaryConfig(misleading_descriptions={"order.get@1.2.0": "Misleading."})
    ws = repo.workspace()
    m = mutate(CanaryKind.DESCRIPTION_MISLEAD, config, ws.inputs, {})
    descriptions = {d.model.key: d.model.description for d in m.inputs.definitions if d.model}
    assert descriptions["order.get@1.2.0"] == "Misleading."
    assert m.tools == {"order.get"}


_LAYER_FAIL = RunRecord(
    index=1,
    outcome=RunOutcome.FAIL,
    failure=LayerFailure(layer=Layer.STATE, expected="shipped", actual="in_progress"),
)
_HARNESS_ERROR = RunRecord(index=1, outcome=RunOutcome.ERROR, error=ErrorKind.HARNESS_ERROR)


def _suite(runs: tuple[RunRecord, ...] = (_LAYER_FAIL,), **verdicts: TaskVerdict) -> SuiteResults:
    at = datetime(2026, 9, 26, tzinfo=UTC)
    return SuiteResults(
        run_id="r",
        git_sha=None,
        started_at=at,
        finished_at=at,
        agents=(),
        judge_model=None,
        judge_calibrated=None,
        token_budget=None,
        usage=TokenUsage(),
        budget_exceeded=False,
        tasks=tuple(
            TaskResult(
                task_id=t,
                agent="a",
                tools=(),
                input_digest="d",
                runs_required=1,
                threshold=1,
                verdict=v,
                runs=runs,
                quarantined=False,
                quarantine_recommended=False,
            )
            for t, v in verdicts.items()
        ),
    )


def test_canary_status() -> None:
    base = _suite(a=TaskVerdict.PASS, b=TaskVerdict.FAIL)
    assert status([], base, None) is CanaryStatus.NO_COVERAGE
    assert status(["b"], base, _suite(b=TaskVerdict.FAIL)) is CanaryStatus.BASELINE_FAILING
    assert status(["a"], base, None) is CanaryStatus.BASELINE_FAILING
    assert status(["a"], base, _suite(a=TaskVerdict.FAIL)) is CanaryStatus.CAUGHT
    assert status(["a"], base, _suite(a=TaskVerdict.INCOMPLETE)) is CanaryStatus.INCOMPLETE
    assert status(["a"], base, _suite(a=TaskVerdict.PASS)) is CanaryStatus.MISSED


def test_an_errored_mutated_run_is_not_caught() -> None:
    base = _suite(a=TaskVerdict.PASS)
    errored = _suite(runs=(_HARNESS_ERROR,), a=TaskVerdict.FAIL)
    assert status(["a"], base, errored) is CanaryStatus.INCOMPLETE


def test_unparseable_timestamps_are_left_alone(repo: Repo) -> None:
    repo.write(FIXTURE, {"order_id": "1", "status": "in_progress", "created_at": "later"})
    body, tools, _ = _mutate(repo, CanaryKind.TIMEZONE_SHIFT)
    assert (body["created_at"], tools) == ("later", frozenset())
