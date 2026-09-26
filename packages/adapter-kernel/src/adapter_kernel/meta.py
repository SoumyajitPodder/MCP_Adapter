"""Response ``_meta`` returned with every tool result (brief §3.4)."""

from pydantic import Field

from adapter_kernel._model import KernelModel


class ResponseMeta(KernelModel):
    """Adapter metadata attached to every result, success or failure."""

    correlation_id: str = Field(min_length=1, description="Echoed back (§8).")
    absorbed: tuple[str, ...] = Field(
        default=(), description="Drift changes absorbed on this call (§3)."
    )
    warnings: tuple[str, ...] = Field(
        default=(), description="Deprecation or pending-review notices (§3/§4)."
    )
    replayed: bool = Field(
        default=False, description="True if the result came from the idempotency store (§7)."
    )
    adapter_version: str | None = Field(
        default=None, description="Adapter version that served the call (§4)."
    )
