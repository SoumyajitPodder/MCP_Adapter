"""Tool definitions as an agent sees them: description and inputs (brief §3.6, DESIGN R-020).

Interim stand-in for the tool registry. Kept outside the released-contract lock on purpose: a
description-only edit needs no version bump (§6.9), but it must re-run the affected tasks.
Inputs use the canonical contract's field vocabulary and render to JSON Schema for tools/list.
"""

import hashlib
from datetime import datetime
from typing import Self

from pydantic import Field, JsonValue, model_validator

from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.shape import canonical_json
from adapter_verify.access.domain.semver import Version
from adapter_verify.common.model import FrozenModel
from adapter_verify.contract_ci.domain.contracts import ContractType


class InputField(FrozenModel):
    """One tool argument."""

    name: str = Field(min_length=1, description="Argument name.")
    type: ContractType = Field(description="Canonical type.")
    values: tuple[str, ...] | None = Field(default=None, description="Allowed values for enums.")
    required: bool = Field(default=True, description="Whether the argument must be present.")
    description: str = Field(default="", description="What the argument means, for the agent.")

    @model_validator(mode="after")
    def _values_for_enums(self) -> Self:
        if (self.type is ContractType.ENUM) != bool(self.values):
            msg = "values are required exactly for enum fields"
            raise ValueError(msg)
        return self


_JSON_TYPES = {
    ContractType.STRING: "string",
    ContractType.INTEGER: "integer",
    ContractType.NUMBER: "number",
    ContractType.BOOLEAN: "boolean",
    ContractType.ENUM: "string",
    ContractType.DATETIME: "string",
}


class ToolDefinition(FrozenModel):
    """What ``tools/list`` shows an agent for one tool version."""

    tool: str = Field(min_length=1, description="Tool name.")
    version: str = Field(description="Tool version.")
    description: str = Field(min_length=1, description="Agent-facing description.")
    inputs: tuple[InputField, ...] = Field(default=(), description="Arguments.")

    @model_validator(mode="after")
    def _valid(self) -> Self:
        Version.parse(self.version)
        names = [f.name for f in self.inputs]
        if len(names) != len(set(names)):
            msg = "input names must be unique"
            raise ValueError(msg)
        return self

    @property
    def key(self) -> str:
        return f"{self.tool}@{self.version}"

    def input_schema(self) -> JsonObject:
        """JSON Schema of the arguments: closed, every declared field typed."""
        properties: dict[str, JsonValue] = {}
        for f in self.inputs:
            prop: dict[str, JsonValue] = {"type": _JSON_TYPES[f.type]}
            if f.values:
                prop["enum"] = list(f.values)
            if f.type is ContractType.DATETIME:
                prop["format"] = "date-time"
            if f.description:
                prop["description"] = f.description
            properties[f.name] = prop
        return {
            "type": "object",
            "properties": properties,
            "required": [f.name for f in self.inputs if f.required],
            "additionalProperties": False,
        }

    def description_digest(self) -> str:
        return hashlib.sha256(self.description.encode()).hexdigest()

    def schema_digest(self) -> str:
        return hashlib.sha256(canonical_json(self.input_schema())).hexdigest()


def argument_error(definition: ToolDefinition, arguments: JsonObject) -> str | None:
    """The first argument path that violates the definition, or None. Paths only, never values."""
    declared = {f.name: f for f in definition.inputs}
    for name in sorted(arguments):
        if name not in declared:
            return name
    for f in definition.inputs:
        if f.name not in arguments:
            if f.required:
                return f.name
            continue
        if not conforms(f.type, f.values, arguments[f.name]):
            return f.name
    return None


def conforms(kind: ContractType, values: tuple[str, ...] | None, value: JsonValue) -> bool:
    """Whether a JSON value is a valid instance of a canonical type."""
    checks = {
        ContractType.STRING: isinstance(value, str),
        ContractType.ENUM: isinstance(value, str) and value in (values or ()),
        ContractType.DATETIME: isinstance(value, str) and is_timestamp(value),
        ContractType.BOOLEAN: isinstance(value, bool),
        ContractType.INTEGER: isinstance(value, int) and not isinstance(value, bool),
        ContractType.NUMBER: isinstance(value, int | float) and not isinstance(value, bool),
    }
    return checks[kind]


def is_timestamp(value: str) -> bool:
    """An ISO 8601 timestamp with an explicit offset."""
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False
