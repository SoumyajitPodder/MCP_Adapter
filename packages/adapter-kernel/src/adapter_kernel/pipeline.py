"""Pipeline stage interface (brief §3.5) and the results that flow through it."""

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import Field

from adapter_kernel._model import KernelModel
from adapter_kernel.context import CallContext
from adapter_kernel.errors import AdapterError
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.meta import ResponseMeta


class DeliveryStatus(StrEnum):
    """What a connector knows about whether a request reached the backend (DESIGN.md R-002).

    Idempotency (§7) decides a record's final state from this, never from the error code.
    """

    NOT_SENT = "not_sent"
    SENT_NO_RESPONSE = "sent_no_response"
    ACKED = "acked"
    REJECTED_NO_EFFECT = "rejected_no_effect"

    @property
    def upstream_effect_possible(self) -> bool:
        return self in {DeliveryStatus.SENT_NO_RESPONSE, DeliveryStatus.ACKED}


class ToolRequest(KernelModel):
    """Validated-shape tool arguments. Carriers such as correlation ID live in the context."""

    arguments: JsonObject = Field(description="Tool arguments as sent by the agent.")


class ToolSuccess(KernelModel):
    """A successful tool call."""

    kind: Literal["success"] = Field(default="success", description="Discriminator.")
    content: JsonObject = Field(description="Canonical tool output.")
    meta: ResponseMeta = Field(description="Adapter metadata.")


class ToolFailure(KernelModel):
    """A failed tool call. The error is always a member of the shared enum."""

    kind: Literal["failure"] = Field(default="failure", description="Discriminator.")
    error: AdapterError = Field(description="Agent-facing error.")
    meta: ResponseMeta = Field(description="Adapter metadata.")


class ToolResult(KernelModel):
    """Result passed back up the stage chain.

    ``delivery`` is internal: it is removed at the MCP boundary and never reaches an agent.
    It is None when the call never reached a connector (e.g. rejected by an earlier stage).
    """

    outcome: Annotated[ToolSuccess | ToolFailure, Field(discriminator="kind")] = Field(
        description="Success or failure, discriminated by kind."
    )
    delivery: DeliveryStatus | None = Field(
        default=None, description="Connector-reported delivery status. Internal only."
    )


class Next(Protocol):
    """The remainder of the pipeline, as seen by a stage."""

    async def __call__(self, ctx: CallContext, request: ToolRequest) -> ToolResult: ...


class Stage(Protocol):
    """One pipeline stage. May short-circuit by returning a failure; may not reorder others."""

    name: str

    async def __call__(
        self, ctx: CallContext, request: ToolRequest, call_next: Next
    ) -> ToolResult: ...
