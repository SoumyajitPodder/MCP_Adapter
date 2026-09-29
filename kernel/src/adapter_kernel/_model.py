"""Base model shared by every kernel value object."""

from pydantic import BaseModel, ConfigDict


class KernelModel(BaseModel):
    """Immutable, strict, closed model. Every kernel boundary type derives from this."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
