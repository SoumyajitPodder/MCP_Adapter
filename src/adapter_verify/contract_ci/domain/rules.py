"""Diff rules as data (brief §5.5). The rule book is loaded from ``rules.yaml``."""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from adapter_kernel.classification import Classification
from adapter_verify.common.model import FrozenModel


class CheckKind(StrEnum):
    UPSTREAM = "upstream"
    CANONICAL = "canonical"


class Direction(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    BOTH = "both"
    NONE = "none"


class Rule(FrozenModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    direction: Direction
    detects: str = Field(min_length=1)
    upstream: Classification | None = Field(description="Classification in the upstream check.")
    canonical: Classification | None = Field(description="Classification in the canonical check.")
    presence: bool = Field(
        default=False,
        description="Field-presence rule: COMPATIBLE is downgraded on INFERRED shapes.",
    )
    compatible_with_minor_bump: bool = Field(
        default=False, description="Canonical check: COMPATIBLE when the revision is a MINOR bump."
    )
    rationale: str = Field(min_length=1)

    def classification(self, check: CheckKind) -> Classification | None:
        return self.upstream if check is CheckKind.UPSTREAM else self.canonical


class RuleBook(FrozenModel):
    rules: tuple[Rule, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            msg = "duplicate rule id"
            raise ValueError(msg)
        return self

    def get(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        msg = f"unknown rule {rule_id}"
        raise KeyError(msg)
