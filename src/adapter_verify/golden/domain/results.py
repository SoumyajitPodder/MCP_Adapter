"""Suite results (brief §6.10), schema version 1. Stored as a JSON artifact per suite run.

Any change to these models bumps ``RESULTS_SCHEMA_VERSION``.
"""

from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from adapter_verify.common.model import FrozenModel
from adapter_verify.golden.domain.assertions import CallRecord, LayerFailure
from adapter_verify.golden.domain.judge import JudgeVerdict

RESULTS_SCHEMA_VERSION: Final = 1


class RunOutcome(StrEnum):
    PASS = "pass"  # noqa: S105 - an enum label, not a credential
    FAIL = "fail"
    ERROR = "error"
    JUDGE_UNCALIBRATED = "judge_uncalibrated"
    NOT_JUDGED = "not_judged"
    """Every deterministic layer passed; the answer layer needs a judge and none is configured."""


class ErrorKind(StrEnum):
    BUDGET_EXCEEDED = "budget_exceeded"
    EGRESS_BLOCKED = "egress_blocked"
    SENTINEL_LEAK = "sentinel_leak"
    HARNESS_ERROR = "harness_error"
    JUDGE_ERROR = "judge_error"


class TaskVerdict(StrEnum):
    PASS = "pass"  # noqa: S105 - an enum label, not a credential
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    """Could still pass: the runs that didn't pass were not judged or the judge was uncalibrated."""


class TokenUsage(FrozenModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class AgentIdentity(FrozenModel):
    agent_id: str = Field(description="Agent under test.")
    harness: str = Field(description="Harness kind.")
    model: str | None = Field(description="Model ID the harness reports, if any.")


class RunRecord(FrozenModel):
    index: int = Field(ge=1, description="Run number within the task.")
    outcome: RunOutcome
    failure: LayerFailure | None = Field(default=None, description="Set when outcome is fail.")
    error: ErrorKind | None = Field(default=None, description="Set when outcome is error.")
    detail: str | None = Field(default=None, description="Log-safe detail for errors.")
    calls: tuple[CallRecord, ...] = Field(default=(), description="Calls, as the sandbox saw them.")
    answer: str | None = Field(default=None, description="Final answer (synthetic data only).")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    judge: JudgeVerdict | None = Field(default=None)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.outcome is RunOutcome.FAIL) != (self.failure is not None):
            msg = "failure is set exactly for failed runs"
            raise ValueError(msg)
        if (self.outcome is RunOutcome.ERROR) != (self.error is not None):
            msg = "error is set exactly for errored runs"
            raise ValueError(msg)
        return self


class TaskResult(FrozenModel):
    task_id: str
    agent: str
    tools: tuple[str, ...]
    input_digest: str = Field(description="Digest of every input the task depends on.")
    runs_required: int = Field(ge=1)
    threshold: int = Field(ge=1)
    verdict: TaskVerdict
    runs: tuple[RunRecord, ...]
    quarantined: bool = Field(description="Listed in quarantine.yaml: reported, doesn't gate.")
    quarantine_recommended: bool = Field(
        description="Failed its threshold twice in a row on the same input digest."
    )
    selected_because: tuple[str, ...] = Field(default=(), description="Why the task ran.")


class SuiteResults(FrozenModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    git_sha: str | None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    agents: tuple[AgentIdentity, ...]
    judge_model: str | None = Field(description="Judge model, when a judge is configured.")
    judge_calibrated: bool | None = Field(description="None when no judge is configured.")
    token_budget: int | None = Field(description="Suite token budget, if set.")
    usage: TokenUsage
    budget_exceeded: bool
    tasks: tuple[TaskResult, ...]

    def task(self, task_id: str) -> TaskResult | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)
