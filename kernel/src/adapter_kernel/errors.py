"""Adapter-wide error taxonomy (brief §3.2, with DESIGN.md R-003 applied)."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import Field, model_validator

from adapter_kernel._model import KernelModel


class RetryPolicy(StrEnum):
    """Whether, and when, an agent may retry the same call with the same idempotency key."""

    NEVER = "never"
    IMMEDIATE = "immediate"
    AFTER_DELAY = "after_delay"


class ErrorCode(StrEnum):
    """Every error an agent can receive. Nothing else is ever returned."""

    INVALID_INPUT = "INVALID_INPUT"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    VERSION_NOT_PERMITTED = "VERSION_NOT_PERMITTED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    DUPLICATE_IN_PROGRESS = "DUPLICATE_IN_PROGRESS"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    DRIFT_BLOCKED = "DRIFT_BLOCKED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    INTERNAL = "INTERNAL"


class ErrorSpec(KernelModel):
    """Fixed properties of an error code."""

    retry: RetryPolicy = Field(description="Retry guidance returned to the agent.")
    owner: str = Field(description="Brief section that raises this code.")
    agent_message: str = Field(
        min_length=1, description="Fixed agent-facing text. Never interpolated."
    )


ERROR_SPECS: Mapping[ErrorCode, ErrorSpec] = MappingProxyType(
    {
        ErrorCode.INVALID_INPUT: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="shared",
            agent_message="The tool arguments do not match the tool's input schema.",
        ),
        ErrorCode.NOT_AUTHORIZED: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§9",
            agent_message="This agent is not authorized to call this tool.",
        ),
        ErrorCode.VERSION_NOT_PERMITTED: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§9",
            agent_message="This agent is not permitted to call this version of the tool.",
        ),
        ErrorCode.SCOPE_VIOLATION: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§9",
            agent_message="This agent has read-only scope and cannot call a state-changing tool.",
        ),
        ErrorCode.IDEMPOTENCY_KEY_REQUIRED: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§7",
            agent_message="This tool changes state and requires an idempotency key.",
        ),
        ErrorCode.IDEMPOTENCY_KEY_CONFLICT: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§7",
            agent_message="This idempotency key was already used with different arguments.",
        ),
        ErrorCode.DUPLICATE_IN_PROGRESS: ErrorSpec(
            retry=RetryPolicy.AFTER_DELAY,
            owner="§7",
            agent_message="An identical call is still in progress. Retry after the given delay.",
        ),
        ErrorCode.RECONCILIATION_PENDING: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§7",
            agent_message=(
                "The outcome of an earlier identical call is unknown and is being resolved "
                "by a person. Do not retry."
            ),
        ),
        ErrorCode.DRIFT_BLOCKED: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§3",
            agent_message="The backend changed in a way the adapter cannot safely absorb.",
        ),
        ErrorCode.UPSTREAM_UNAVAILABLE: ErrorSpec(
            retry=RetryPolicy.IMMEDIATE,
            owner="§3/§4",
            # R-003: only returned when the request provably never left; otherwise §7
            # converts the failure to RECONCILIATION_PENDING.
            agent_message="The backend is unavailable. The request was not sent.",
        ),
        ErrorCode.CONTRACT_VIOLATION: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="§2",
            agent_message="The backend returned data that does not match the tool's contract.",
        ),
        ErrorCode.INTERNAL: ErrorSpec(
            retry=RetryPolicy.NEVER,
            owner="shared",
            agent_message="An internal error occurred. Quote the correlation ID when reporting it.",
        ),
    }
)


class AdapterError(KernelModel):
    """An error as returned to an agent."""

    code: ErrorCode = Field(description="Member of the shared error enum.")
    retry_after_ms: int | None = Field(
        default=None,
        gt=0,
        description="Suggested delay. Set only when the code's retry policy is after_delay.",
    )

    @model_validator(mode="after")
    def _retry_after_matches_policy(self) -> Self:
        needs_delay = ERROR_SPECS[self.code].retry is RetryPolicy.AFTER_DELAY
        if needs_delay != (self.retry_after_ms is not None):
            msg = "retry_after_ms must be set exactly when the retry policy is after_delay"
            raise ValueError(msg)
        return self

    @property
    def spec(self) -> ErrorSpec:
        return ERROR_SPECS[self.code]
