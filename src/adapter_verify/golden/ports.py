"""Ports the golden-task runner depends on (brief §6.2, §6.3, §6.5)."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol

from pydantic import Field

from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.pipeline import ToolFailure, ToolSuccess
from adapter_verify.common.model import FrozenModel
from adapter_verify.golden.domain.assertions import CallRecord, SandboxState
from adapter_verify.golden.domain.judge import AnswerEvidence, JudgeVerdict
from adapter_verify.golden.domain.results import TokenUsage
from adapter_verify.golden.domain.tasks import AgentConfig, GoldenTask, RunLimits


class ToolView(FrozenModel):
    """One tool as ``tools/list`` shows it to the agent."""

    name: str
    version: str
    description: str
    input_schema: JsonObject


class ToolCall(FrozenModel):
    """A call the agent makes. Version and key travel as MCP request ``_meta`` (M0 §5)."""

    tool: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)
    version: str | None = None
    idempotency_key: str | None = None


class AgentRun(FrozenModel):
    """What the harness reports after a run. Calls are recorded by the sandbox, not trusted."""

    answer: str | None = Field(description="Final answer text.")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str | None = Field(default=None, description="Model that answered, if reported.")


class ToolCaller(Protocol):
    async def __call__(self, call: ToolCall) -> ToolSuccess | ToolFailure:
        """Run one call through the sandbox pipeline. Never raises for agent mistakes."""
        ...


class AgentHarness(Protocol):
    """The agent under test (§6.3)."""

    @property
    def kind(self) -> str: ...

    async def run(
        self,
        prompt: str,
        tools: Sequence[ToolView],
        call_tool: ToolCaller,
        limits: RunLimits,
    ) -> AgentRun: ...


class HarnessUnavailableError(Exception):
    """No harness of the configured kind exists (in M5a: none do)."""


class JudgeUnavailableError(Exception):
    """The judge gave no verdict. ``str()`` is a short, secret-free reason, e.g.
    ``429 RESOURCE_EXHAUSTED``."""


class HarnessFactory(Protocol):
    def __call__(self, config: AgentConfig) -> AgentHarness:
        """Build a harness. Raises HarnessUnavailableError."""
        ...


class Judge(Protocol):
    """Grades a final answer against a rubric (§6.5). First adapter: M5b."""

    @property
    def model(self) -> str: ...

    async def grade(self, rubric: str, evidence: AnswerEvidence) -> JudgeVerdict:
        """May raise, preferably JudgeUnavailableError; the runner turns any failure into
        JUDGE_ERROR."""
        ...


class SandboxSession(Protocol):
    """One isolated run: fresh stub backend, stores and sinks."""

    async def list_tools(self) -> list[ToolView]: ...

    async def call_tool(self, call: ToolCall) -> tuple[CallRecord, ToolSuccess | ToolFailure]: ...

    def state(self) -> SandboxState: ...

    def observed(self) -> str:
        """Everything the pipeline's sinks recorded, for the sentinel check."""
        ...


class SandboxFactory(Protocol):
    def __call__(self, task: GoldenTask, run_label: str) -> SandboxSession: ...


class EgressGuard(Protocol):
    def guard(self) -> AbstractContextManager[list[str]]:
        """Block outbound connections while open; yields the blocked destinations."""
        ...
