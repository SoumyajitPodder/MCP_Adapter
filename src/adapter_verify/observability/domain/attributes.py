"""The closed set of span names and span attributes (brief §8.2, §8.5).

There is no free-form attribute API. A value can only reach a span through a named, typed
field below, so a payload body cannot become a span attribute by accident (§8.4).
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import Field

from adapter_kernel.classification import Classification
from adapter_kernel.errors import ErrorCode
from adapter_verify.common.model import FrozenModel

# Mirrored names follow open-telemetry/semantic-conventions-genai at this commit (M1-Q3).
# The repo has no tagged releases; bump deliberately, in a reviewed PR, after re-verifying.
SEMCONV_GENAI_COMMIT = "8ffdf568e1b4391a99adb081db16e8102e36918e"

ADAPTER_PREFIX = "adapter."


class SpanName(StrEnum):
    """Every span the adapter emits. Low cardinality by construction."""

    TOOL_CALL = "tool.call"
    ACCESS_AUTHENTICATE = "access.authenticate"
    ACCESS_CHECK = "access.check"
    LIFECYCLE_GATE = "lifecycle.gate"
    INPUT_VALIDATE = "input.validate"
    IDEMPOTENCY_RESERVE = "idempotency.reserve"
    BACKEND_CALL = "backend.call"
    DRIFT_ABSORB = "drift.absorb"
    OUTPUT_VALIDATE = "output.validate"


class Outcome(StrEnum):
    """Result of a stage or call."""

    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


class SpanAttributes(FrozenModel):
    """Everything a span may carry. Identifiers and metadata only, never bodies."""

    correlation_id: str | None = Field(default=None, description="Business join key.")
    agent_id: str | None = Field(default=None, description="Authenticated agent.")
    tool: str | None = Field(default=None, description="Tool name.")
    semantic_version: str | None = Field(default=None, description="Resolved tool version.")
    outcome: Outcome | None = Field(default=None, description="Stage outcome.")
    error_code: ErrorCode | None = Field(default=None, description="Shared error code.")
    rule_id: str | None = Field(default=None, description="Access rule that decided (§9).")
    adapter_version: str | None = Field(default=None, description="Serving adapter version.")
    adapter_state: str | None = Field(default=None, description="Lifecycle state (§4).")
    failing_path: str | None = Field(
        default=None, description="Schema path that failed validation. A path, never a value."
    )
    idempotency_state: str | None = Field(default=None, description="§7 record state.")
    replayed: bool | None = Field(default=None, description="Result served from §7 store.")
    backend: str | None = Field(default=None, description="Upstream backend id.")
    interface: str | None = Field(default=None, description="rest, soap, file or queue.")
    latency_ms: float | None = Field(default=None, ge=0, description="Stage latency.")
    drift_classification: Classification | None = Field(
        default=None, description="Drift classification (§3)."
    )
    mapping_id: str | None = Field(default=None, description="Mapping used (§3).")
    absorbed_fields: tuple[str, ...] | None = Field(
        default=None, description="Field paths absorbed by drift handling."
    )
    quarantine_id: str | None = Field(default=None, description="Output quarantine ref (§2).")


type AttributeValue = str | bool | int | float | Sequence[str]


def otel_attributes(name: SpanName, attrs: SpanAttributes) -> Mapping[str, AttributeValue]:
    """Flatten to OTel attributes: ``adapter.*`` is the source of truth, plus verified mirrors.

    ``gen_ai.tool.call.arguments`` / ``.result`` exist upstream but carry bodies; they are
    deliberately never emitted.
    """
    out: dict[str, AttributeValue] = {
        f"{ADAPTER_PREFIX}{key}": value
        for key, value in attrs.model_dump(mode="json", exclude_none=True).items()
    }
    if name is SpanName.TOOL_CALL:
        out["gen_ai.operation.name"] = "execute_tool"
        out["mcp.method.name"] = "tools/call"
        if attrs.tool is not None:
            out["gen_ai.tool.name"] = attrs.tool
    return out
