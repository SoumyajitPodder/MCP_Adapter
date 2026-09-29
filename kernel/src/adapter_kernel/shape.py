"""Source-independent shape model (brief §5.2, with DESIGN.md R-009 applied).

Every upstream source (OpenAPI, WSDL, file layout, queue schema, observed traffic) is
normalized into a ``Shape`` so one differ serves them all. Shapes are stored in canonical
order, so equal shapes serialize identically and hash identically.
"""

import hashlib
import json
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, field_serializer, field_validator, model_validator

from adapter_kernel._model import KernelModel


class FieldType(StrEnum):
    """Normalized field type across all source kinds."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"
    DATETIME = "datetime"
    ENUM = "enum"
    NULL = "null"


class SourceKind(StrEnum):
    """Kind of upstream artifact a shape was extracted from."""

    OPENAPI = "openapi"
    WSDL = "wsdl"
    FILE = "file"
    QUEUE = "queue"
    OBSERVED = "observed"
    CANONICAL = "canonical"


class ProvenanceKind(StrEnum):
    """Strength of the evidence behind a shape (brief 5.3)."""

    DECLARED = "declared"
    OBSERVED = "observed"
    INFERRED = "inferred"


class FieldShape(KernelModel):
    """One field of an operation's input or output."""

    path: str = Field(min_length=1, description='Field path, e.g. "order.items[].sku".')
    type: FieldType = Field(description="Normalized field type.")
    required: bool = Field(description="Field must be present.")
    nullable: bool = Field(description="Field may be null when present.")
    enum_values: frozenset[str] | None = Field(
        default=None, description="Allowed values. Set exactly when type is enum."
    )
    format: str | None = Field(
        default=None, description="date-time, a regex, or a fixed-width layout reference."
    )

    @model_validator(mode="after")
    def _enum_values_match_type(self) -> Self:
        is_enum = self.type is FieldType.ENUM
        if is_enum != (self.enum_values is not None):
            msg = "enum_values must be set exactly when type is enum"
            raise ValueError(msg)
        if is_enum and not self.enum_values:
            msg = "an enum field needs at least one value"
            raise ValueError(msg)
        return self

    @field_serializer("enum_values")
    def _sorted_enum_values(self, values: frozenset[str] | None) -> list[str] | None:
        return None if values is None else sorted(values)


def _canonical_fields(fields: tuple[FieldShape, ...]) -> tuple[FieldShape, ...]:
    ordered = tuple(sorted(fields, key=lambda f: f.path))
    paths = [f.path for f in ordered]
    if len(set(paths)) != len(paths):
        msg = "duplicate field path"
        raise ValueError(msg)
    return ordered


class OperationShape(KernelModel):
    """One operation of a source, with fields in canonical order."""

    name: str = Field(min_length=1, description="Operation name within the source.")
    inputs: tuple[FieldShape, ...] = Field(description="Input fields, sorted by path.")
    outputs: tuple[FieldShape, ...] = Field(description="Output fields, sorted by path.")
    errors: frozenset[str] = Field(description="Declared error identifiers.")

    @field_validator("inputs", "outputs")
    @classmethod
    def _sort_fields(cls, fields: tuple[FieldShape, ...]) -> tuple[FieldShape, ...]:
        return _canonical_fields(fields)

    @field_serializer("errors")
    def _sorted_errors(self, errors: frozenset[str]) -> list[str]:
        return sorted(errors)


class Provenance(KernelModel):
    """Where a shape came from and how much to trust it."""

    kind: ProvenanceKind = Field(description="How strong the evidence behind the shape is.")
    sample_count: int | None = Field(
        ge=1, description="Samples behind an observed or inferred shape; None when declared."
    )
    extractor: str = Field(min_length=1, description="Extractor that produced the shape.")
    extractor_version: str = Field(min_length=1, description="Extractor version.")
    extracted_at: AwareDatetime = Field(description="Extraction time, timezone-aware.")

    @model_validator(mode="after")
    def _sample_count_matches_kind(self) -> Self:
        declared = self.kind is ProvenanceKind.DECLARED
        if declared != (self.sample_count is None):
            msg = "sample_count must be None exactly when provenance is declared"
            raise ValueError(msg)
        return self


class Shape(KernelModel):
    """Normalized shape of one upstream source. The unit of a baseline."""

    source_id: str = Field(
        min_length=1,
        description='Stable source identifier, e.g. "order-management.rest.get-order".',
    )
    source_kind: SourceKind = Field(description="Kind of source the shape was extracted from.")
    source_version: str = Field(min_length=1, description="Version of the source artifact.")
    operations: tuple[OperationShape, ...] = Field(description="Operations, sorted by name.")
    provenance: Provenance = Field(description="Evidence behind the shape. Excluded from the hash.")

    @field_validator("operations")
    @classmethod
    def _sort_operations(cls, ops: tuple[OperationShape, ...]) -> tuple[OperationShape, ...]:
        ordered = tuple(sorted(ops, key=lambda o: o.name))
        names = [o.name for o in ordered]
        if len(set(names)) != len(names):
            msg = "duplicate operation name"
            raise ValueError(msg)
        return ordered

    def content_hash(self) -> str:
        """SHA-256 over ``source_id`` and ``operations`` only (R-009).

        Excludes version, kind and provenance, so the hash changes exactly when the
        structure a differ compares changes.
        """
        payload = {
            "source_id": self.source_id,
            "operations": [op.model_dump(mode="json") for op in self.operations],
        }
        return hashlib.sha256(canonical_json(payload)).hexdigest()


def canonical_json(value: object) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, UTF-8.

    Sufficient for shapes, which contain no floats. Argument fingerprints (§7.3) use
    RFC 8785 instead.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


__all__ = [
    "FieldShape",
    "FieldType",
    "OperationShape",
    "Provenance",
    "ProvenanceKind",
    "Shape",
    "SourceKind",
    "canonical_json",
]
