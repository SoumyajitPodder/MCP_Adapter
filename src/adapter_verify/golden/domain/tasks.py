"""Golden task files (brief §6.1), agent configs and the quarantine list (§6.6, R-013)."""

import re
from enum import StrEnum
from typing import Final, Self

from pydantic import Field, model_validator

from adapter_kernel.jsontypes import JsonObject
from adapter_verify.access.domain.policy import AGENT_ID_PATTERN
from adapter_verify.common.model import FrozenModel

TASK_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,63}$"
SYNTHETIC_FIXTURE: Final = re.compile(r"^fixtures/golden/[A-Za-z0-9._-]+\.synthetic\.json$")
_DATE: Final = r"^\d{4}-\d{2}-\d{2}$"


class ArgsMatch(StrEnum):
    EXACT = "exact"
    SUBSET = "subset"


class FixtureRef(FrozenModel):
    """What the stub backend returns for calls to one tool (canonical output, synthetic)."""

    tool: str = Field(min_length=1, description="Tool the fixture answers for.")
    source_id: str | None = Field(
        default=None, description="Upstream source it stands for (§5); used once connectors exist."
    )
    payload: str = Field(description="Repo-relative path: fixtures/golden/<name>.synthetic.json.")
    when: JsonObject | None = Field(
        default=None,
        description="Serve only for calls whose arguments contain these values; None: any call.",
    )


class ExpectedCall(FrozenModel):
    tool: str = Field(min_length=1, description="Tool the agent must call.")
    semantic_version: str | None = Field(
        default=None, description="Required resolved version; None accepts any."
    )
    args: JsonObject = Field(default_factory=dict, description="Expected arguments.")
    args_match: ArgsMatch = Field(
        default=ArgsMatch.EXACT,
        description="exact: arguments equal; subset: these keys with these values. JSON equality.",
    )


class ExpectedState(FrozenModel):
    writes_performed: int | None = Field(
        default=None, ge=0, description="State-changing calls the stub backend acknowledged."
    )
    calls_by_tool: dict[str, int] = Field(
        default_factory=dict, description="Calls that reached the stub backend, per tool."
    )


class ExpectedAnswer(FrozenModel):
    rubric: str = Field(min_length=1, description="What a correct final answer says (§6.5).")


class GoldenTask(FrozenModel):
    """One golden task (§6.1). Unknown keys are rejected."""

    task_id: str = Field(pattern=TASK_ID_PATTERN, description="Unique; equals the file name.")
    agent: str = Field(pattern=AGENT_ID_PATTERN, description="Agent under test; the directory.")
    description: str = Field(min_length=1, description="What the task proves.")
    prompt: str = Field(min_length=1, description="What the agent is asked.")
    fixtures: tuple[FixtureRef, ...] = Field(default=(), description="Stub backend answers.")
    expect_calls: tuple[ExpectedCall, ...] = Field(default=(), description="Required calls.")
    ordered: bool = Field(
        default=True,
        description="expect_calls must occur in this order (other calls may interleave).",
    )
    exact_calls: bool = Field(default=False, description="No calls beyond expect_calls.")
    expect_no_calls: tuple[str, ...] = Field(
        default=(), description="Tools the agent must not even attempt."
    )
    expect_state: ExpectedState | None = Field(default=None, description="Stub backend end state.")
    expect_answer: ExpectedAnswer | None = Field(default=None, description="Judged final answer.")
    runs: int = Field(ge=1, le=10, description="Repeats per suite run.")
    threshold: int = Field(ge=1, description="Passing runs needed.")
    tags: tuple[str, ...] = Field(default=(), description="Free-form labels.")

    def tools(self) -> frozenset[str]:
        """Every tool the task touches."""
        return frozenset(
            [f.tool for f in self.fixtures]
            + [c.tool for c in self.expect_calls]
            + list(self.expect_no_calls)
            + list(self.expect_state.calls_by_tool if self.expect_state else [])
        )


class RunLimits(FrozenModel):
    """Per-run limits (§6.3). Crossing any is BUDGET_EXCEEDED, never retried."""

    max_tool_calls: int = Field(default=20, ge=1, le=500, description="Calls per run.")
    timeout_s: float = Field(default=120.0, gt=0, le=3_600, description="Wall-clock per run.")
    max_tokens: int | None = Field(
        default=None, ge=1, description="Tokens per run, as reported by the harness."
    )


class AgentConfig(FrozenModel):
    """``golden_tasks/<agent_id>/agent.yaml``: how to run one agent under test."""

    agent_id: str = Field(pattern=AGENT_ID_PATTERN, description="Agent; equals the directory.")
    harness: str = Field(min_length=1, description="Harness kind that runs the agent.")
    model: str | None = Field(default=None, description="Pinned model ID, recorded with results.")
    system_prompt: str | None = Field(
        default=None, min_length=1, description="Instructions for an LLM-backed agent."
    )
    limits: RunLimits = Field(default_factory=RunLimits, description="Per-run limits.")

    @model_validator(mode="after")
    def _reference_is_complete(self) -> Self:
        if self.harness == "reference" and not (self.model and self.system_prompt):
            msg = "the reference harness needs model and system_prompt"
            raise ValueError(msg)
        return self


class Waiver(FrozenModel):
    """Lets named tools be promoted while a task that touches them is quarantined (R-013)."""

    tools: tuple[str, ...] = Field(min_length=1, description="Tools released from the block.")
    approved_by: str = Field(min_length=1, description="Who accepted the risk.")
    reason: str = Field(min_length=1, description="Why.")


class QuarantineEntry(FrozenModel):
    task_id: str = Field(pattern=TASK_ID_PATTERN, description="Quarantined task.")
    owner: str = Field(min_length=1, description="Who fixes it.")
    reason: str = Field(min_length=1, description="Why it is quarantined.")
    since: str = Field(pattern=_DATE, description="YYYY-MM-DD, quoted.")
    waiver: Waiver | None = Field(default=None, description="Promotion waiver, if any.")


class QuarantineList(FrozenModel):
    """``golden_tasks/quarantine.yaml``. Changed only by reviewed PR (CODEOWNERS)."""

    entries: tuple[QuarantineEntry, ...] = Field(default=())

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [e.task_id for e in self.entries]
        if len(ids) != len(set(ids)):
            msg = "a task is quarantined twice"
            raise ValueError(msg)
        return self

    def get(self, task_id: str) -> QuarantineEntry | None:
        return next((e for e in self.entries if e.task_id == task_id), None)
