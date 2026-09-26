import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_verify.golden.domain.assertions import (
    CallRecord,
    Layer,
    LayerFailure,
    SandboxState,
    check_calls,
    check_sequence,
    check_state,
    describe_calls,
    json_equal,
)
from adapter_verify.golden.domain.definitions import ToolDefinition, argument_error
from adapter_verify.golden.domain.judge import JudgeVerdict, calibration_misses
from adapter_verify.golden.domain.report import render
from adapter_verify.golden.domain.results import (
    ErrorKind,
    RunOutcome,
    RunRecord,
    SuiteResults,
    TaskResult,
    TaskVerdict,
    TokenUsage,
)
from adapter_verify.golden.domain.select import Layout, input_digest, select
from adapter_verify.golden.domain.tasks import GoldenTask, QuarantineList
from adapter_verify.golden.domain.verdict import GateExit, gate, task_verdict, touches
from tests.unit.golden.support import DEFINITIONS, read_task, write_task

pytestmark = pytest.mark.unit


def _json[M: BaseModel](model: type[M], data: Any) -> M:
    """Validate like the loaders do: strict models take JSON, not Python lists."""
    return model.model_validate_json(json.dumps(data))


AT = datetime(2026, 9, 26, tzinfo=UTC)
GET = _json(ToolDefinition, DEFINITIONS["order.get/1.2.0"])
CANCEL = _json(ToolDefinition, DEFINITIONS["order.cancel/1.0.0"])
TASK = _json(GoldenTask, read_task())


def _call(tool: str = "order.get", error: ErrorCode | None = None, **args: str) -> CallRecord:
    arguments: JsonObject = {**args} if args else {"order_id": "1"}
    return CallRecord(tool=tool, version="1.2.0", arguments=arguments, error_code=error)


def test_definition_schema_and_argument_checks() -> None:
    schema = CANCEL.input_schema()
    assert schema["required"] == ["order_id"]
    assert schema["additionalProperties"] is False
    assert schema["properties"] == {
        "order_id": {"type": "string"},
        "reason": {"type": "string", "enum": ["customer", "fraud"]},
    }
    assert argument_error(CANCEL, {"order_id": "1"}) is None
    assert argument_error(CANCEL, {"order_id": "1", "reason": "fraud"}) is None
    assert argument_error(CANCEL, {"order_id": "1", "reason": "bored"}) == "reason"
    assert argument_error(CANCEL, {}) == "order_id"
    assert argument_error(CANCEL, {"order_id": 1}) == "order_id"
    assert argument_error(CANCEL, {"order_id": "1", "extra": True}) == "extra"


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("integer", 3, True),
        ("number", 2.5, "2.5"),
        ("boolean", False, 0),
        ("datetime", "2026-09-26T10:00:00+00:00", "2026-09-26"),
    ],
)
def test_input_types(kind: str, good: object, bad: object) -> None:
    d = _json(
        ToolDefinition,
        {
            "tool": "t",
            "version": "1.0.0",
            "description": "d",
            "inputs": [{"name": "x", "type": kind}],
        },
    )
    assert argument_error(d, {"x": good}) is None  # type: ignore[dict-item]
    assert argument_error(d, {"x": bad}) == "x"  # type: ignore[dict-item]


def test_definition_validation_and_digests() -> None:
    with pytest.raises(ValidationError):
        _json(ToolDefinition, {**DEFINITIONS["order.get/1.2.0"], "version": "one"})
    with pytest.raises(ValidationError):
        _json(
            ToolDefinition,
            {**DEFINITIONS["order.get/1.2.0"], "inputs": [{"name": "a", "type": "string"}] * 2},
        )
    with pytest.raises(ValidationError):
        _json(
            ToolDefinition,
            {**DEFINITIONS["order.get/1.2.0"], "inputs": [{"name": "a", "type": "enum"}]},
        )
    reworded = GET.model_copy(update={"description": "Something else."})
    assert reworded.description_digest() != GET.description_digest()
    assert reworded.schema_digest() == GET.schema_digest()


def test_json_equality() -> None:
    assert json_equal({"a": 1, "b": [1.0]}, {"b": [1], "a": 1.0})
    assert not json_equal({"a": True}, {"a": 1})
    assert not json_equal({"a": float("nan")}, {"a": float("nan")})
    assert describe_calls([]) == "no calls"
    assert "<not canonical JSON>" in describe_calls(
        [CallRecord(tool="t", version=None, arguments={"x": float("inf")}, error_code=None)]
    )


def test_calls_sequence_and_state_layers() -> None:
    subset = _json(GoldenTask, write_task())
    assert check_calls(subset, [_call("order.cancel", order_id="1", reason="fraud")]) is None
    pinned = _json(
        GoldenTask,
        read_task(
            expect_calls=[
                {"tool": "order.get", "semantic_version": "2.0.0", "args": {"order_id": "1"}}
            ]
        ),
    )
    assert check_calls(pinned, [_call()]) is not None

    two = _json(
        GoldenTask,
        read_task(
            expect_calls=[
                {"tool": "order.get", "args": {"order_id": "1"}},
                {"tool": "order.get", "args": {"order_id": "2"}},
            ],
            exact_calls=True,
        ),
    )
    in_order = [_call(order_id="1"), _call(order_id="2")]
    assert check_calls(two, in_order) is None
    assert check_sequence(two, in_order) is None
    reversed_ = list(reversed(in_order))
    failure = check_sequence(two, reversed_)
    assert failure is not None
    assert failure.expected.startswith("in order")
    unordered = two.model_copy(update={"ordered": False})
    assert check_sequence(unordered, reversed_) is None
    extra = check_sequence(two, [*in_order, _call(order_id="3")])
    assert extra is not None
    assert extra.expected == "exactly 2 call(s)"

    state = SandboxState(writes_performed=1, calls_by_tool={"order.cancel": 2})
    assert check_state(TASK, state) is not None  # expects no writes
    counted = check_state(subset, state)
    assert counted is not None
    assert counted.actual == "2 time(s)"
    assert check_state(TASK.model_copy(update={"expect_state": None}), state) is None


def test_tools_touched() -> None:
    task = _json(GoldenTask, read_task(expect_state={"calls_by_tool": {"order.list": 0}}))
    assert task.tools() == {"order.get", "order.cancel", "order.list"}


def _run(outcome: RunOutcome, index: int = 1) -> RunRecord:
    extra: dict[str, object] = {}
    if outcome is RunOutcome.FAIL:
        extra["failure"] = LayerFailure(layer=Layer.CALLS, expected="e", actual="a")
    if outcome is RunOutcome.ERROR:
        extra["error"] = ErrorKind.HARNESS_ERROR
    return RunRecord.model_validate({"index": index, "outcome": outcome, **extra})


def test_run_record_consistency() -> None:
    with pytest.raises(ValidationError):
        RunRecord(index=1, outcome=RunOutcome.FAIL)
    with pytest.raises(ValidationError):
        RunRecord(index=1, outcome=RunOutcome.ERROR)


@pytest.mark.parametrize(
    ("outcomes", "verdict"),
    [
        ([RunOutcome.PASS, RunOutcome.PASS, RunOutcome.FAIL], TaskVerdict.PASS),
        ([RunOutcome.PASS, RunOutcome.FAIL, RunOutcome.ERROR], TaskVerdict.FAIL),
        ([RunOutcome.PASS, RunOutcome.NOT_JUDGED, RunOutcome.FAIL], TaskVerdict.INCOMPLETE),
        ([RunOutcome.JUDGE_UNCALIBRATED] * 3, TaskVerdict.INCOMPLETE),
    ],
)
def test_task_verdicts(outcomes: list[RunOutcome], verdict: TaskVerdict) -> None:
    assert task_verdict(2, [_run(o, i) for i, o in enumerate(outcomes, 1)]) is verdict


def _result(task_id: str, verdict: TaskVerdict, digest: str) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        agent="reader",
        tools=("order.get",),
        input_digest=digest,
        runs_required=1,
        threshold=1,
        verdict=verdict,
        runs=(),
        quarantined=False,
        quarantine_recommended=False,
    )


def _suite(*tasks: TaskResult) -> SuiteResults:
    return SuiteResults(
        run_id="r",
        git_sha=None,
        started_at=AT,
        finished_at=AT,
        agents=(),
        judge_model=None,
        judge_calibrated=None,
        token_budget=None,
        usage=TokenUsage(),
        budget_exceeded=False,
        tasks=tasks,
    )


def _gate(results: SuiteResults, quarantine: QuarantineList | None = None, **kw: str) -> GateExit:
    return gate(
        kw.get("tool", "order.get"),
        kw.get("version", "1.2.0"),
        tasks=[TASK],
        digests={TASK.task_id: "d1"},
        results=results,
        quarantine=quarantine or QuarantineList(),
        default_version="1.2.0",
    ).exit


def test_gate_fails_closed() -> None:
    assert _gate(_suite(_result(TASK.task_id, TaskVerdict.PASS, "d1"))) is GateExit.PASS
    assert _gate(_suite(_result(TASK.task_id, TaskVerdict.PASS, "old"))) is GateExit.FAIL
    assert _gate(_suite(_result(TASK.task_id, TaskVerdict.INCOMPLETE, "d1"))) is GateExit.FAIL
    assert _gate(_suite()) is GateExit.FAIL
    assert _gate(_suite(), tool="inventory.snapshot") is GateExit.FAIL  # no coverage
    quarantined = _json(
        QuarantineList,
        {
            "entries": [
                {"task_id": TASK.task_id, "owner": "o", "reason": "r", "since": "2026-09-26"}
            ]
        },
    )
    assert _gate(_suite(), quarantined) is GateExit.QUARANTINE_BLOCK
    waived = _json(
        QuarantineList,
        {
            "entries": [
                {
                    "task_id": TASK.task_id,
                    "owner": "o",
                    "reason": "r",
                    "since": "2026-09-26",
                    "waiver": {"tools": ["order.get"], "approved_by": "owner", "reason": "known"},
                }
            ]
        },
    )
    assert _gate(_suite(), waived) is GateExit.PASS  # the owner accepted the risk in review
    with pytest.raises(ValidationError, match="twice"):
        _json(QuarantineList, {"entries": [waived.entries[0].model_dump()] * 2})


def test_touches_respects_pinned_versions() -> None:
    pinned = _json(
        GoldenTask, read_task(expect_calls=[{"tool": "order.get", "semantic_version": "2.0.0"}])
    )
    assert touches(pinned, "order.get", "2.0.0", "1.2.0")
    assert not touches(pinned, "order.get", "1.2.0", "1.2.0")
    assert touches(TASK, "order.get", "1.2.0", "1.2.0")
    assert not touches(TASK, "order.get", "2.0.0", "1.2.0")
    assert not touches(TASK, "service.get", "1.0.0", "1.0.0")


def test_selection_by_changed_files() -> None:
    other = _json(GoldenTask, write_task())
    tasks = [TASK, other]
    layout = Layout()
    assert select(tasks, ["README.md", "golden_tasks/quarantine.yaml"], layout) == {}
    assert select(tasks, ["catalog/definitions/order.get/1.2.0.yaml"], layout) == {
        TASK.task_id: ["definition of order.get changed"]
    }
    by_fixture = select(tasks, ["fixtures/golden/order_1_cancel.synthetic.json"], layout)
    assert list(by_fixture) == [other.task_id]
    assert list(select(tasks, ["golden_tasks/writer/agent.yaml"], layout)) == [other.task_id]
    assert list(select(tasks, ["policies/reader.yaml"], layout)) == [TASK.task_id]
    assert list(select(tasks, ["golden_tasks/reader/order-inflight.yaml"], layout)) == [
        TASK.task_id
    ]
    everything = select(tasks, [f"src/m{i}.py" for i in range(5)], layout)
    assert set(everything) == {TASK.task_id, other.task_id}
    assert everything[TASK.task_id] == [
        "full suite: 5 adapter file(s) changed (src/m0.py, src/m1.py, src/m2.py, ...)"
    ]


def test_input_digest_tracks_every_dependency() -> None:
    base = input_digest(
        TASK, [GET, CANCEL], {"fixtures/golden/order_1_inprog.synthetic.json": "a"}, None, None
    )
    reworded = GET.model_copy(update={"description": "Changed."})
    assert (
        input_digest(
            TASK,
            [reworded, CANCEL],
            {"fixtures/golden/order_1_inprog.synthetic.json": "a"},
            None,
            None,
        )
        != base
    )
    assert (
        input_digest(
            TASK, [GET, CANCEL], {"fixtures/golden/order_1_inprog.synthetic.json": "b"}, None, None
        )
        != base
    )
    assert (
        input_digest(
            TASK,
            [GET, CANCEL],
            {"fixtures/golden/order_1_inprog.synthetic.json": "a"},
            None,
            {"p": 1},
        )
        != base
    )


def test_judge_verdicts_and_calibration() -> None:
    assert JudgeVerdict(score=0.5, passed=True, reasons=("r",)).consistent
    assert not JudgeVerdict(score=0.4, passed=True, reasons=("r",)).consistent
    with pytest.raises(ValidationError):
        JudgeVerdict(score=1.5, passed=True, reasons=("r",))
    assert calibration_misses([], []) == ["<no calibration cases>"]


def test_report_names_the_layer_the_change_and_the_recommendation() -> None:
    failed = TaskResult(
        task_id="t1",
        agent="reader",
        tools=("order.get",),
        input_digest="d",
        runs_required=2,
        threshold=2,
        verdict=TaskVerdict.FAIL,
        runs=(
            RunRecord(
                index=1,
                outcome=RunOutcome.FAIL,
                failure=LayerFailure(
                    layer=Layer.CALLS, expected="order.get | x", actual="no calls"
                ),
            ),
            RunRecord(
                index=2,
                outcome=RunOutcome.ERROR,
                error=ErrorKind.BUDGET_EXCEEDED,
                detail="too long",
            ),
        ),
        quarantined=False,
        quarantine_recommended=True,
        selected_because=("definition of order.get changed",),
    )
    incomplete = _result("t2", TaskVerdict.INCOMPLETE, "d").model_copy(
        update={"runs": (_run(RunOutcome.NOT_JUDGED),)}
    )
    suite = _suite(failed, incomplete).model_copy(
        update={"budget_exceeded": True, "token_budget": 10}
    )
    text = render(suite, {"order.get@1.2.0": "-old\n+new\n"})
    assert "0 pass, 1 fail, 1 incomplete" in text
    assert "**calls** layer failed" in text
    assert "expected: order.get \\| x" in text
    assert "error **budget_exceeded**: too long" in text
    assert "run 1: not_judged" in text
    assert "No answer judge is configured" in text
    assert "token budget (10) exceeded" in text
    assert "### Quarantine recommended" in text
    assert "```diff\n-old\n+new\n```" in text
    uncalibrated = _suite().model_copy(update={"judge_model": "j", "judge_calibrated": False})
    assert "failed calibration" in render(uncalibrated, {})
