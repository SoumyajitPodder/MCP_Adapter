"""Golden-task runner (brief §6.3 to §6.6): run each task in a fresh sandbox, apply the assertion
layers in order, aggregate runs into verdicts."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.access.fakes import SENTINEL_SECRET_PREFIX
from adapter_verify.common.ports import Clock
from adapter_verify.golden.domain.assertions import (
    CallRecord,
    Layer,
    LayerFailure,
    check_calls,
    check_sequence,
    check_state,
)
from adapter_verify.golden.domain.judge import (
    NO_CALIBRATION_CASES,
    AnswerEvidence,
    CalibrationCase,
    CalibrationMiss,
    JudgeVerdict,
    calibration_miss,
)
from adapter_verify.golden.domain.results import (
    AgentIdentity,
    ErrorKind,
    RunOutcome,
    RunRecord,
    SuiteResults,
    TaskResult,
    TokenUsage,
)
from adapter_verify.golden.domain.tasks import AgentConfig, GoldenTask, QuarantineList, RunLimits
from adapter_verify.golden.domain.verdict import quarantine_recommended, task_verdict
from adapter_verify.golden.ports import (
    AgentHarness,
    AgentRun,
    EgressGuard,
    HarnessFactory,
    HarnessFailedError,
    Judge,
    JudgeUnavailableError,
    ModelUnavailableError,
    SandboxFactory,
    SandboxSession,
    ToolCall,
)
from adapter_verify.observability.domain.exceptions import safe_exception_summary


@dataclass(frozen=True)
class SuitePlan:
    """One suite run: which tasks, with which configs, compared against what."""

    run_id: str
    git_sha: str | None
    tasks: Sequence[GoldenTask]
    agents: Mapping[str, AgentConfig]
    digests: Mapping[str, str]
    quarantine: QuarantineList
    selected_because: Mapping[str, Sequence[str]] = field(default_factory=dict)
    previous: SuiteResults | None = None
    token_budget: int | None = None


class _Recorder:
    """The ToolCaller handed to the agent: records every call, enforces the call limit."""

    def __init__(self, session: SandboxSession, limits: RunLimits) -> None:
        self._session = session
        self._limits = limits
        self.records: list[CallRecord] = []
        self.results: list[ToolSuccess | ToolFailure] = []
        self.exceeded = False

    async def __call__(self, call: ToolCall) -> ToolSuccess | ToolFailure:
        if len(self.records) >= self._limits.max_tool_calls:
            self.exceeded = True
            return ToolFailure(
                error=AdapterError(code=ErrorCode.INTERNAL),
                meta=ResponseMeta(correlation_id="golden:limit"),
            )
        record, outcome = await self._session.call_tool(call)
        self.records.append(record)
        self.results.append(outcome)
        return outcome


@dataclass(frozen=True)
class _NoVerdict:
    """Why the judge gave no verdict; ``outage`` when its model was unavailable (D-094)."""

    reason: str
    outage: bool = False


def _error(index: int, kind: ErrorKind, detail: str, **kw: object) -> RunRecord:
    return RunRecord.model_validate(
        {"index": index, "outcome": RunOutcome.ERROR, "error": kind, "detail": detail, **kw}
    )


class GoldenRunner:
    def __init__(  # noqa: PLR0913 - one parameter per port
        self,
        *,
        sandboxes: SandboxFactory,
        harnesses: HarnessFactory,
        judge: Judge | None,
        calibration: Sequence[CalibrationCase],
        egress: EgressGuard,
        clock: Clock,
    ) -> None:
        self._sandboxes = sandboxes
        self._harnesses = harnesses
        self._judge = judge
        self._calibration = tuple(calibration)
        self._egress = egress
        self._clock = clock

    async def run(self, plan: SuitePlan) -> SuiteResults:
        """Raises HarnessUnavailableError before any run if an agent can't be run."""
        started = self._clock.now()
        tasks = sorted(plan.tasks, key=lambda t: t.task_id)
        harnesses = {a: self._harnesses(plan.agents[a]) for a in sorted({t.agent for t in tasks})}
        calibrated = await self._calibrate() if any(t.expect_answer for t in tasks) else None
        usage, exceeded = TokenUsage(), False
        identities: dict[str, AgentIdentity] = {}
        results: list[TaskResult] = []
        for task in tasks:
            config, harness = plan.agents[task.agent], harnesses[task.agent]
            runs: list[RunRecord] = []
            for index in range(1, task.runs + 1):
                if plan.token_budget is not None and usage.total >= plan.token_budget:
                    exceeded = True
                    runs.append(
                        _error(index, ErrorKind.BUDGET_EXCEEDED, "suite token budget spent")
                    )
                    continue
                record, model = await self._run_once(task, config, harness, index, calibrated)
                usage += record.usage
                runs.append(record)
                identities[task.agent] = AgentIdentity(
                    agent_id=task.agent, harness=harness.kind, model=model or config.model
                )
            results.append(self._task_result(plan, task, runs))
        return SuiteResults(
            run_id=plan.run_id,
            git_sha=plan.git_sha,
            started_at=started,
            finished_at=self._clock.now(),
            agents=tuple(identities[a] for a in sorted(identities)),
            judge_model=None if self._judge is None else self._judge.model,
            judge_calibrated=calibrated,
            token_budget=plan.token_budget,
            usage=usage,
            budget_exceeded=exceeded,
            tasks=tuple(results),
        )

    def _task_result(
        self, plan: SuitePlan, task: GoldenTask, runs: Sequence[RunRecord]
    ) -> TaskResult:
        result = TaskResult(
            task_id=task.task_id,
            agent=task.agent,
            tools=tuple(sorted(task.tools())),
            input_digest=plan.digests[task.task_id],
            runs_required=task.runs,
            threshold=task.threshold,
            verdict=task_verdict(task.threshold, runs),
            runs=tuple(runs),
            quarantined=plan.quarantine.get(task.task_id) is not None,
            quarantine_recommended=False,
            selected_because=tuple(plan.selected_because.get(task.task_id, ())),
        )
        previous = None if plan.previous is None else plan.previous.task(task.task_id)
        return result.model_copy(
            update={"quarantine_recommended": quarantine_recommended(result, previous)}
        )

    async def calibrate(self) -> list[CalibrationMiss] | None:
        """Cases the judge got wrong (§6.5); None without a judge. A judge with no cases is never
        calibrated. Stops at the first judge failure: the rest would fail the same way and spend
        quota."""
        if self._judge is None:
            return None
        if not self._calibration:
            return [NO_CALIBRATION_CASES]
        misses = []
        for case in self._calibration:
            verdict = await self._grade(case.rubric, case.evidence)
            reason = verdict.reason if isinstance(verdict, _NoVerdict) else verdict
            miss = calibration_miss(case, reason)
            if miss is not None:
                misses.append(miss)
                if miss.judge_error is not None:
                    break
        return misses

    async def _calibrate(self) -> bool | None:
        misses = await self.calibrate()
        return None if misses is None else not misses

    async def _grade(self, rubric: str, evidence: AnswerEvidence) -> JudgeVerdict | _NoVerdict:
        """The verdict, or why there is none."""
        if self._judge is None:  # pragma: no cover - callers check first
            return _NoVerdict("no judge")
        try:
            return await self._judge.grade(rubric, evidence)
        except ModelUnavailableError as exc:
            return _NoVerdict(str(exc), outage=True)
        except JudgeUnavailableError as exc:
            return _NoVerdict(str(exc))
        except Exception as exc:  # noqa: BLE001 - any judge failure is a missing verdict
            return _NoVerdict(safe_exception_summary(exc))

    async def _run_once(
        self,
        task: GoldenTask,
        config: AgentConfig,
        harness: AgentHarness,
        index: int,
        calibrated: bool | None,  # noqa: FBT001 - three-valued, not a flag
    ) -> tuple[RunRecord, str | None]:
        session = self._sandboxes(task, f"golden:{task.task_id}:{index}")
        attempt = _Attempt(
            index=index, limits=config.limits, recorder=_Recorder(session, config.limits)
        )
        tools = await session.list_tools()
        with self._egress.guard() as blocked:
            try:
                async with asyncio.timeout(config.limits.timeout_s):
                    attempt.agent_run = await harness.run(
                        task.prompt, tools, attempt.recorder, config.limits
                    )
            except TimeoutError:
                attempt.timed_out = True
            except ModelUnavailableError as exc:
                attempt.outage = f"agent model unavailable: {exc}"
            except HarnessFailedError as exc:
                attempt.failure = str(exc)
            except Exception as exc:  # noqa: BLE001 - any harness failure fails the run
                attempt.failure = safe_exception_summary(exc)
        attempt.blocked = list(blocked)
        model = None if attempt.agent_run is None else attempt.agent_run.model
        early = attempt.early_error()
        if early is not None:
            return early, model
        if SENTINEL_SECRET_PREFIX in attempt.visible_text() + session.observed():
            return attempt.error(ErrorKind.SENTINEL_LEAK, "a sentinel secret surfaced"), model
        failure = (
            check_calls(task, attempt.recorder.records)
            or check_sequence(task, attempt.recorder.records)
            or check_state(task, session.state())
        )
        if failure is not None:
            return attempt.record(RunOutcome.FAIL, failure=failure), model
        return await self._answer_layer(task, attempt, calibrated), model

    async def _answer_layer(
        self,
        task: GoldenTask,
        attempt: "_Attempt",
        calibrated: bool | None,  # noqa: FBT001 - three-valued, not a flag
    ) -> RunRecord:
        """The fourth layer; runs only after every deterministic layer passed (§6.4)."""
        if task.expect_answer is None:
            return attempt.record(RunOutcome.PASS)
        if self._judge is None:
            return attempt.record(RunOutcome.NOT_JUDGED)
        if not calibrated:
            return attempt.record(RunOutcome.JUDGE_UNCALIBRATED)
        rubric = task.expect_answer.rubric
        answer = attempt.answer
        if not answer:
            missing = LayerFailure(layer=Layer.ANSWER, expected=rubric, actual="no final answer")
            return attempt.record(RunOutcome.FAIL, failure=missing)
        return await self._grade_answer(task, attempt, rubric, answer)

    async def _grade_answer(
        self, task: GoldenTask, attempt: "_Attempt", rubric: str, answer: str
    ) -> RunRecord:
        evidence = AnswerEvidence(
            prompt=task.prompt,
            tool_results=tuple(
                o.content if isinstance(o, ToolSuccess) else {"error": o.error.code.value}
                for o in attempt.recorder.results
            ),
            answer=answer,
        )
        verdict = await self._grade(rubric, evidence)
        if isinstance(verdict, _NoVerdict) and verdict.outage:
            return attempt.error(
                ErrorKind.MODEL_UNAVAILABLE, f"judge model unavailable: {verdict.reason}"
            )
        if isinstance(verdict, _NoVerdict):
            return attempt.error(ErrorKind.JUDGE_ERROR, f"judge failed: {verdict.reason}")
        if not verdict.consistent:
            return attempt.error(ErrorKind.JUDGE_ERROR, "judge verdict inconsistent", judge=verdict)
        if not verdict.passed:
            failure = LayerFailure(
                layer=Layer.ANSWER, expected=rubric, actual="; ".join(verdict.reasons)
            )
            return attempt.record(RunOutcome.FAIL, failure=failure, judge=verdict)
        return attempt.record(RunOutcome.PASS, judge=verdict)


@dataclass
class _Attempt:
    """One run in progress: what the agent did and how it ended."""

    index: int
    limits: RunLimits
    recorder: _Recorder
    agent_run: AgentRun | None = None
    blocked: list[str] = field(default_factory=list)
    timed_out: bool = False
    outage: str | None = None
    failure: str | None = None

    @property
    def answer(self) -> str | None:
        return None if self.agent_run is None else self.agent_run.answer

    @property
    def usage(self) -> TokenUsage:
        return TokenUsage() if self.agent_run is None else self.agent_run.usage

    def record(self, outcome: RunOutcome, **extra: object) -> RunRecord:
        return RunRecord.model_validate(
            {
                "index": self.index,
                "outcome": outcome,
                "calls": tuple(self.recorder.records),
                "answer": self.answer,
                "usage": self.usage,
                **extra,
            }
        )

    def error(self, kind: ErrorKind, detail: str, **extra: object) -> RunRecord:
        return self.record(RunOutcome.ERROR, error=kind, detail=detail, **extra)

    def early_error(self) -> RunRecord | None:
        """Ways a run ends before assessment, most serious first."""
        limits = self.limits
        if self.blocked:
            return self.error(ErrorKind.EGRESS_BLOCKED, ", ".join(sorted(set(self.blocked))))
        reasons = [
            (self.timed_out, f"run exceeded {limits.timeout_s} s"),
            (self.recorder.exceeded, f"more than {limits.max_tool_calls} tool calls"),
            (
                limits.max_tokens is not None and self.usage.total > limits.max_tokens,
                f"{self.usage.total} tokens > {limits.max_tokens}",
            ),
        ]
        over = next((why for hit, why in reasons if hit), None)
        if over is not None:
            return self.error(ErrorKind.BUDGET_EXCEEDED, over)
        if self.outage is not None:
            return self.error(ErrorKind.MODEL_UNAVAILABLE, self.outage)
        if self.failure is not None:
            return self.error(ErrorKind.HARNESS_ERROR, self.failure)
        return None

    def visible_text(self) -> str:
        """Everything the agent saw or said."""
        return json.dumps(
            [
                [r.model_dump(mode="json") for r in self.recorder.records],
                [o.model_dump(mode="json") for o in self.recorder.results],
                self.answer,
            ]
        )
