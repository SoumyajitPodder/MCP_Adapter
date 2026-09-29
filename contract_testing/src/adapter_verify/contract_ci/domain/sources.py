"""Upstream source definitions: what to extract, from which samples, feeding which tools."""

from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from adapter_kernel.shape import SourceKind
from adapter_verify.common.model import FrozenModel
from adapter_verify.contract_ci.domain.extract import ColumnSpec


class SampleFile(FrozenModel):
    path: str = Field(min_length=1, description="Relative to the repository root.")
    captured_at: AwareDatetime | None = Field(
        default=None,
        description="Capture time; required for CSV files (JSONL lines carry their own).",
    )


class SourceConfig(FrozenModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: SourceKind
    version: str = Field(min_length=1, description="Upstream version the samples come from.")
    operation: str = Field(min_length=1)
    tools: tuple[str, ...] = Field(min_length=1, description="Tools this source feeds.")
    enum_paths: tuple[str, ...] = Field(default=(), description="Fields whose values form an enum.")
    samples: tuple[SampleFile, ...] = Field(min_length=1)
    layout: tuple[ColumnSpec, ...] | None = Field(default=None, description="Declared CSV layout.")
    delimiter: str = Field(default=",", min_length=1, max_length=1)

    @model_validator(mode="after")
    def _kind_rules(self) -> Self:
        if self.kind is SourceKind.OBSERVED:
            if self.layout is not None:
                msg = "observed sources have no declared layout"
                raise ValueError(msg)
        elif self.kind is SourceKind.FILE:
            if not self.layout:
                msg = "file sources need a declared layout"
                raise ValueError(msg)
            if any(s.captured_at is None for s in self.samples):
                msg = "file samples need captured_at"
                raise ValueError(msg)
        else:
            msg = "only observed and file sources are supported in Phase 1 (DESIGN Session 11)"
            raise ValueError(msg)
        return self
