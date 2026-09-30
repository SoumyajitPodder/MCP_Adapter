"""In-package fakes for the golden ports (DESIGN.md X-10): self-tests run without an LLM."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.golden.domain.judge import AnswerEvidence, JudgeVerdict
from adapter_verify.golden.domain.results import TokenUsage
from adapter_verify.golden.domain.tasks import RunLimits
from adapter_verify.golden.ports import AgentRun, ToolCall, ToolCaller, ToolView, UsageMeter


class ScriptedAgent:
    """Makes a fixed list of calls and gives a fixed answer, whatever the tools return.

    Proves the machinery, not agent behavior: it cannot react to a description or a result.
    """

    kind = "scripted"

    def __init__(
        self,
        calls: Sequence[ToolCall],
        answer: str | None = "done",
        usage: TokenUsage | None = None,
        model: str | None = "scripted-v1",
    ) -> None:
        self._calls = tuple(calls)
        self._answer = answer
        self._usage = usage or TokenUsage()
        self._model = model
        self.seen_tools: list[list[ToolView]] = []
        self.results: list[ToolSuccess | ToolFailure] = []

    async def run(
        self,
        prompt: str,
        tools: Sequence[ToolView],
        call_tool: ToolCaller,
        limits: RunLimits,
        meter: UsageMeter | None = None,
    ) -> AgentRun:
        del prompt, limits
        if meter is not None:
            meter.add(self._usage)
        self.seen_tools.append(list(tools))
        for call in self._calls:
            self.results.append(await call_tool(call))
        return AgentRun(answer=self._answer, usage=self._usage, model=self._model)


class ScriptedJudge:
    """Returns verdicts in order, or raises the given exception."""

    def __init__(
        self, verdicts: Sequence[JudgeVerdict | Exception], model: str = "scripted-judge"
    ) -> None:
        self._verdicts = list(verdicts)
        self._model = model
        self.graded: list[tuple[str, AnswerEvidence]] = []

    @property
    def model(self) -> str:
        return self._model

    async def grade(self, rubric: str, evidence: AnswerEvidence) -> JudgeVerdict:
        self.graded.append((rubric, evidence))
        verdict = self._verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


class RecordingEgressGuard:
    """Blocks nothing; reports the destinations it was told were attempted (``attempts``)."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    @contextmanager
    def guard(self) -> Iterator[list[str]]:
        blocked: list[str] = []
        try:
            yield blocked
        finally:
            blocked.extend(self.attempts)
