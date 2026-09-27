import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from adapter_kernel.errors import ErrorCode
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.access.fakes import SENTINEL_SECRET_PREFIX
from adapter_verify.golden.domain.assertions import Layer
from adapter_verify.golden.domain.judge import AnswerEvidence, CalibrationCase, JudgeVerdict
from adapter_verify.golden.domain.results import ErrorKind, RunOutcome, TaskVerdict, TokenUsage
from adapter_verify.golden.domain.tasks import RunLimits
from adapter_verify.golden.fakes import RecordingEgressGuard, ScriptedAgent, ScriptedJudge
from adapter_verify.golden.ports import (
    AgentRun,
    JudgeUnavailableError,
    ToolCall,
    ToolCaller,
    ToolView,
)
from tests.unit.golden.support import Repo, read_task, run, write_task

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

GET = ToolCall(tool="order.get", arguments={"order_id": "1"})
CANCEL = ToolCall(tool="order.cancel", arguments={"order_id": "1"}, idempotency_key="k-1")
GOOD = JudgeVerdict(score=0.9, passed=True, reasons=("says in progress",))
BAD = JudgeVerdict(score=0.1, passed=False, reasons=("promised a date",))
CASES = [
    CalibrationCase(
        case_id="good",
        rubric="r",
        evidence=AnswerEvidence(prompt="p", tool_results=(), answer="a"),
        expected_pass=True,
    )
]


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo.create(tmp_path)


async def test_read_task_passes_every_deterministic_layer(repo: Repo) -> None:
    agent = ScriptedAgent([GET], answer="It is in progress.")
    results = await run(repo, agent, ["order-inflight"])
    (task,) = results.tasks
    assert task.verdict is TaskVerdict.PASS
    assert [r.outcome for r in task.runs] == [RunOutcome.PASS] * 3
    assert task.runs[0].calls[0].version == "1.2.0"
    # tools/list shows only what the agent may call (§9.5), with its definition.
    assert [v.name for v in agent.seen_tools[0]] == ["order.get"]
    assert agent.seen_tools[0][0].input_schema["required"] == ["order_id"]
    assert isinstance(agent.results[0], ToolSuccess)
    assert agent.results[0].content == {"order_id": "1", "status": "in_progress"}


async def test_write_task_goes_through_idempotency_and_counts_the_write(repo: Repo) -> None:
    agent = ScriptedAgent([CANCEL])
    results = await run(repo, agent, ["order-cancel"])
    assert results.tasks[0].verdict is TaskVerdict.PASS


async def test_write_without_key_is_rejected_and_fails_the_state_layer(repo: Repo) -> None:
    agent = ScriptedAgent([ToolCall(tool="order.cancel", arguments={"order_id": "1"})])
    results = await run(repo, agent, ["order-cancel"])
    run1 = results.tasks[0].runs[0]
    assert run1.calls[0].error_code is ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    assert run1.failure is not None
    assert run1.failure.layer is Layer.STATE
    assert results.tasks[0].verdict is TaskVerdict.FAIL


async def test_missing_call_fails_the_calls_layer_with_expected_and_actual(repo: Repo) -> None:
    agent = ScriptedAgent([ToolCall(tool="order.get", arguments={"order_id": "2"})])
    results = await run(repo, agent, ["order-inflight"])
    failure = results.tasks[0].runs[0].failure
    assert failure is not None
    assert failure.layer is Layer.CALLS
    assert '{"order_id":"1"}' in failure.expected
    assert '{"order_id":"2"}' in failure.actual


async def test_denied_attempt_at_a_forbidden_tool_fails_the_sequence_layer(repo: Repo) -> None:
    agent = ScriptedAgent([GET, CANCEL])
    results = await run(repo, agent, ["order-inflight"])
    run1 = results.tasks[0].runs[0]
    assert run1.calls[1].error_code is ErrorCode.NOT_AUTHORIZED  # access control still applies
    assert run1.failure is not None
    assert run1.failure.layer is Layer.SEQUENCE


async def test_invalid_arguments_are_rejected_by_the_definition(repo: Repo) -> None:
    agent = ScriptedAgent([ToolCall(tool="order.get", arguments={"id": "1"})])
    results = await run(repo, agent, ["order-inflight"])
    assert results.tasks[0].runs[0].calls[0].error_code is ErrorCode.INVALID_INPUT


async def test_unknown_tool_is_recorded_without_a_version(repo: Repo) -> None:
    agent = ScriptedAgent([ToolCall(tool="order.delete"), GET])
    results = await run(repo, agent, ["order-inflight"])
    call = results.tasks[0].runs[0].calls[0]
    assert (call.version, call.error_code) == (None, ErrorCode.NOT_AUTHORIZED)


async def test_answer_layer_without_a_judge_is_not_judged(repo: Repo) -> None:
    repo.put_task(read_task(expect_answer={"rubric": "says in progress"}))
    results = await run(repo, ScriptedAgent([GET]), ["order-inflight"])
    task = results.tasks[0]
    assert task.verdict is TaskVerdict.INCOMPLETE
    assert {r.outcome for r in task.runs} == {RunOutcome.NOT_JUDGED}
    assert results.judge_calibrated is None


async def test_calibrated_judge_grades_the_answer(repo: Repo) -> None:
    repo.put_task(read_task(expect_answer={"rubric": "says in progress"}, runs=2, threshold=2))
    judge = ScriptedJudge([GOOD, GOOD, BAD])
    results = await run(
        repo,
        ScriptedAgent([GET], answer="In progress."),
        ["order-inflight"],
        judge=judge,
        calibration=CASES,
    )
    task = results.tasks[0]
    assert results.judge_calibrated is True
    assert [r.outcome for r in task.runs] == [RunOutcome.PASS, RunOutcome.FAIL]
    assert task.runs[1].failure is not None
    assert task.runs[1].failure.actual == "promised a date"
    assert task.verdict is TaskVerdict.FAIL
    rubric, evidence = judge.graded[1]
    assert rubric == "says in progress"
    assert evidence.tool_results == ({"order_id": "1", "status": "in_progress"},)


async def test_uncalibrated_judge_is_never_trusted(repo: Repo) -> None:
    repo.put_task(read_task(expect_answer={"rubric": "r"}))
    judge = ScriptedJudge([BAD])  # misclassifies the known-good case
    results = await run(
        repo, ScriptedAgent([GET]), ["order-inflight"], judge=judge, calibration=CASES
    )
    assert results.judge_calibrated is False
    assert {r.outcome for r in results.tasks[0].runs} == {RunOutcome.JUDGE_UNCALIBRATED}
    assert results.tasks[0].verdict is TaskVerdict.INCOMPLETE


@pytest.mark.parametrize(
    ("verdict", "detail"),
    [
        (JudgeUnavailableError("429 RESOURCE_EXHAUSTED"), "judge failed: 429 RESOURCE_EXHAUSTED"),
        (RuntimeError("judge down"), "judge failed: builtins.RuntimeError"),
        (JudgeVerdict(score=0.1, passed=True, reasons=("x",)), "judge verdict inconsistent"),
    ],
)
async def test_judge_failure_or_inconsistency_is_a_judge_error(
    repo: Repo, verdict: JudgeVerdict | Exception, detail: str
) -> None:
    repo.put_task(read_task(expect_answer={"rubric": "r"}, runs=1, threshold=1))
    judge = ScriptedJudge([GOOD, verdict])
    results = await run(
        repo, ScriptedAgent([GET]), ["order-inflight"], judge=judge, calibration=CASES
    )
    run1 = results.tasks[0].runs[0]
    assert run1.error is ErrorKind.JUDGE_ERROR
    assert run1.detail is not None
    assert run1.detail.startswith(detail)
    assert "judge down" not in run1.detail  # exception messages never reach the report


async def test_no_answer_fails_the_answer_layer(repo: Repo) -> None:
    repo.put_task(read_task(expect_answer={"rubric": "r"}, runs=1, threshold=1))
    results = await run(
        repo,
        ScriptedAgent([GET], answer=None),
        ["order-inflight"],
        judge=ScriptedJudge([GOOD]),
        calibration=CASES,
    )
    failure = results.tasks[0].runs[0].failure
    assert failure is not None
    assert (failure.layer, failure.actual) == (Layer.ANSWER, "no final answer")


async def test_calibration_with_no_cases_is_uncalibrated(repo: Repo) -> None:
    repo.put_task(read_task(expect_answer={"rubric": "r"}, runs=1, threshold=1))
    results = await run(repo, ScriptedAgent([GET]), ["order-inflight"], judge=ScriptedJudge([]))
    assert results.judge_calibrated is False


async def test_tool_call_limit_is_a_budget_error(repo: Repo) -> None:
    agent = ScriptedAgent([GET] * 6)  # agent.yaml allows 5
    results = await run(repo, agent, ["order-inflight"])
    run1 = results.tasks[0].runs[0]
    assert (run1.error, run1.detail) == (ErrorKind.BUDGET_EXCEEDED, "more than 5 tool calls")
    assert isinstance(agent.results[-1], ToolFailure)


async def test_token_limit_and_suite_budget(repo: Repo) -> None:
    repo.write(
        "golden_tasks/reader/agent.yaml",
        {"agent_id": "reader", "harness": "scripted", "limits": {"max_tokens": 100}},
    )
    agent = ScriptedAgent([GET], usage=TokenUsage(input_tokens=90, output_tokens=20))
    results = await run(repo, agent, ["order-inflight"])
    assert results.tasks[0].runs[0].error is ErrorKind.BUDGET_EXCEEDED

    cheap = ScriptedAgent([GET], usage=TokenUsage(input_tokens=30, output_tokens=0))
    results = await run(repo, cheap, ["order-inflight"], token_budget=50)
    outcomes = [r.outcome for r in results.tasks[0].runs]
    assert outcomes == [RunOutcome.PASS, RunOutcome.PASS, RunOutcome.ERROR]
    assert results.budget_exceeded is True
    assert results.usage.total == 60


class _SlowAgent(ScriptedAgent):
    async def run(
        self, prompt: str, tools: Sequence[ToolView], call_tool: ToolCaller, limits: RunLimits
    ) -> AgentRun:
        await asyncio.sleep(5)
        return await super().run(prompt, tools, call_tool, limits)


class _BrokenAgent(ScriptedAgent):
    async def run(
        self, prompt: str, tools: Sequence[ToolView], call_tool: ToolCaller, limits: RunLimits
    ) -> AgentRun:
        del prompt, tools, call_tool, limits
        msg = "agent crashed"
        raise RuntimeError(msg)


async def test_timeout_and_harness_errors(repo: Repo) -> None:
    repo.write(
        "golden_tasks/reader/agent.yaml",
        {"agent_id": "reader", "harness": "scripted", "limits": {"timeout_s": 0.05}},
    )
    slow = await run(repo, _SlowAgent([GET]), ["order-inflight"])
    assert slow.tasks[0].runs[0].error is ErrorKind.BUDGET_EXCEEDED
    broken = await run(repo, _BrokenAgent([]), ["order-inflight"])
    run1 = broken.tasks[0].runs[0]
    assert run1.error is ErrorKind.HARNESS_ERROR
    assert run1.detail is not None
    assert "RuntimeError" in run1.detail
    assert "agent crashed" not in run1.detail  # messages can carry data


async def test_blocked_egress_fails_the_run(repo: Repo) -> None:
    egress = RecordingEgressGuard()
    egress.attempts.append("api.example.invalid")
    results = await run(repo, ScriptedAgent([GET]), ["order-inflight"], egress=egress)
    run1 = results.tasks[0].runs[0]
    assert (run1.error, run1.detail) == (ErrorKind.EGRESS_BLOCKED, "api.example.invalid")


@pytest.mark.sentinel_exempt  # the leak is planted on purpose and must be caught by the runner
async def test_sentinel_in_anything_the_agent_says_fails_the_run(repo: Repo) -> None:
    agent = ScriptedAgent([GET], answer=f"token {SENTINEL_SECRET_PREFIX}orders/read")
    results = await run(repo, agent, ["order-inflight"])
    assert results.tasks[0].runs[0].error is ErrorKind.SENTINEL_LEAK


async def test_repeat_failure_on_unchanged_inputs_recommends_quarantine(repo: Repo) -> None:
    failing = ScriptedAgent([])
    first = await run(repo, failing, ["order-inflight"])
    second = await run(repo, failing, ["order-inflight"], previous=first)
    assert first.tasks[0].quarantine_recommended is False
    assert second.tasks[0].quarantine_recommended is True
    repo.put_task(read_task(prompt="Has order 1 shipped?"))  # inputs changed: no recommendation
    third = await run(repo, failing, ["order-inflight"], previous=second)
    assert third.tasks[0].quarantine_recommended is False


async def test_results_record_agent_identity_and_quarantine(repo: Repo) -> None:
    repo.write(
        "golden_tasks/quarantine.yaml",
        {
            "entries": [
                {
                    "task_id": "order-cancel",
                    "owner": "team",
                    "reason": "flaky",
                    "since": "2026-09-26",
                }
            ]
        },
    )
    results = await run(repo, ScriptedAgent([GET]), None)
    assert [a.agent_id for a in results.agents] == ["reader", "writer"]
    assert results.agents[0].model == "scripted-v1"
    cancel = results.task("order-cancel")
    assert cancel is not None
    assert cancel.quarantined is True
    assert write_task()["task_id"] == cancel.task_id
