"""Call context (brief §3.3), split at authentication (DESIGN.md R-008).

``RequestContext`` exists before authentication and has no agent identity.
``CallContext`` exists only after §9 authenticates the caller and the tool version is
resolved (R-006). Stages receive ``CallContext``, so no stage can observe a missing agent.
"""

from pydantic import Field

from adapter_kernel._model import KernelModel
from adapter_kernel.tooldef import Behavior


class RequestContext(KernelModel):
    """Everything known about a call before the caller is authenticated."""

    correlation_id: str = Field(min_length=1, description="Business-level join key (§8).")
    correlation_id_generated: bool = Field(
        description="True when the adapter generated the ID because the caller supplied none."
    )
    trace_id: str = Field(min_length=1, description="Distributed trace ID.")
    tool: str = Field(min_length=1, description="Tool name as called, e.g. order.get.")
    requested_version: str | None = Field(
        description="Version the caller asked for, or None to let the registry resolve it."
    )
    idempotency_key: str | None = Field(
        description="Caller-supplied key. Required for state-changing tools (§7)."
    )


class CallContext(KernelModel):
    """Everything known about a call after authentication and version resolution."""

    request: RequestContext = Field(description="The pre-authentication context.")
    agent_id: str = Field(min_length=1, description="Authenticated agent identity (§9).")
    semantic_version: str = Field(min_length=1, description="Resolved tool version.")
    behavior: Behavior = Field(description="Behavior annotation of the resolved tool.")

    @property
    def correlation_id(self) -> str:
        return self.request.correlation_id

    @property
    def tool(self) -> str:
        return self.request.tool
