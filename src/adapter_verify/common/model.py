"""Base for adapter_verify value objects: the same contract as the kernel's models (brief §2)."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
