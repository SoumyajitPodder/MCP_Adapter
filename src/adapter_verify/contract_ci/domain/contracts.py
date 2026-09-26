"""Canonical tool contracts (M4-Q2): the field-list format of the tool registry prototype,
behind a model that can migrate when the registry's format is final (DESIGN R-020)."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from adapter_kernel.shape import (
    FieldShape,
    FieldType,
    OperationShape,
    Provenance,
    ProvenanceKind,
    Shape,
    SourceKind,
)
from adapter_verify.access.domain.semver import Version
from adapter_verify.common.model import FrozenModel

CONTRACT_EXTRACTOR = "canonical-contract"


class ContractType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    DATETIME = "datetime"


_TYPES = {
    ContractType.STRING: FieldType.STRING,
    ContractType.INTEGER: FieldType.INTEGER,
    ContractType.NUMBER: FieldType.NUMBER,
    ContractType.BOOLEAN: FieldType.BOOLEAN,
    ContractType.ENUM: FieldType.ENUM,
    ContractType.DATETIME: FieldType.DATETIME,
}


class ContractField(FrozenModel):
    name: str = Field(min_length=1)
    type: ContractType
    values: tuple[str, ...] | None = Field(default=None, description="Allowed values for enums.")

    @model_validator(mode="after")
    def _values_for_enums(self) -> Self:
        if (self.type is ContractType.ENUM) != bool(self.values):
            msg = "values are required exactly for enum fields"
            raise ValueError(msg)
        return self

    def shape(self) -> FieldShape:
        return FieldShape(
            path=self.name,
            type=_TYPES[self.type],
            required=True,
            nullable=False,
            enum_values=frozenset(self.values) if self.values else None,
            format="ISO" if self.type is ContractType.DATETIME else None,
        )


class CanonicalContract(FrozenModel):
    tool: str = Field(min_length=1)
    version: str
    fields: tuple[ContractField, ...] = Field(description="Canonical output fields.")
    inputs: tuple[ContractField, ...] = Field(default=(), description="Canonical input fields.")

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        Version.parse(value)
        return value

    @property
    def key(self) -> str:
        return f"{self.tool}@{self.version}"

    @property
    def semver(self) -> Version:
        return Version.parse(self.version)

    def shape(self, extracted_at: datetime) -> Shape:
        return Shape(
            source_id=f"contract:{self.tool}",
            source_kind=SourceKind.CANONICAL,
            source_version=self.version,
            operations=(
                OperationShape(
                    name=self.tool,
                    inputs=tuple(f.shape() for f in self.inputs),
                    outputs=tuple(f.shape() for f in self.fields),
                    errors=frozenset(),
                ),
            ),
            provenance=Provenance(
                kind=ProvenanceKind.DECLARED,
                sample_count=None,
                extractor=CONTRACT_EXTRACTOR,
                extractor_version="1",
                extracted_at=extracted_at,
            ),
        )


class ReleaseLock(FrozenModel):
    """``tool@version`` → content hash of every released contract. Changes only via review."""

    released: dict[str, str] = Field(default_factory=dict)
