"""Call events, schema version 1 (brief §8.3).

Additions beyond §8.3, both needed by the never-sample rules (§8.6): ``behavior`` and
``idempotency_state``. Any change to this model bumps ``schema_version``.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from adapter_kernel.classification import Classification
from adapter_kernel.errors import ErrorCode
from adapter_kernel.tooldef import Behavior
from adapter_verify.common.model import FrozenModel
from adapter_verify.observability.domain.attributes import Outcome, SpanName

EVENT_SCHEMA_VERSION = 1


class CallEvent(FrozenModel):
    """One observable step of one tool call."""

    schema_version: Literal[1] = Field(default=1, description="Bumped on any change.")
    timestamp: AwareDatetime = Field(description="When the step finished.")
    trace_id: str = Field(min_length=1, description="Distributed trace ID.")
    span_id: str = Field(min_length=1, description="Span that emitted the event.")
    correlation_id: str = Field(min_length=1, description="Business-level join key.")
    agent_id: str | None = Field(description="Authenticated agent; None before §9 runs.")
    tool: str = Field(min_length=1, description="Tool name.")
    semantic_version: str | None = Field(description="Resolved version; None before resolution.")
    stage: SpanName = Field(description="Stage that emitted the event.")
    behavior: Behavior | None = Field(default=None, description="Tool behavior, once known.")
    backend: str | None = Field(default=None, description="Upstream backend id.")
    interface: str | None = Field(default=None, description="rest, soap, file or queue.")
    adapter_version: str | None = Field(default=None, description="Serving adapter version.")
    latency_ms: float | None = Field(default=None, ge=0, description="Step latency.")
    outcome: Outcome = Field(description="Step outcome.")
    error_code: ErrorCode | None = Field(
        default=None, description="Set exactly when the outcome is not ok."
    )
    idempotency_key: str | None = Field(default=None, description="Links to §7.")
    idempotency_state: str | None = Field(default=None, description="§7 state change, if any.")
    replayed: bool | None = Field(default=None, description="Served from the §7 store.")
    drift_classification: Classification | None = Field(
        default=None, description="Drift classification (§3), if drift was handled."
    )
    mapping_id: str | None = Field(default=None, description="Mapping used (§3).")
    payload_ref: str | None = Field(
        default=None, description="Pointer into the payload store. Never the body."
    )

    @model_validator(mode="after")
    def _error_code_matches_outcome(self) -> Self:
        if (self.outcome is Outcome.OK) != (self.error_code is None):
            msg = "error_code must be set exactly when outcome is not ok"
            raise ValueError(msg)
        return self
