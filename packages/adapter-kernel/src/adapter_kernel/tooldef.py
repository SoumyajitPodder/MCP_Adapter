"""Tool-definition annotations that sections 5-9 read (brief §3.6).

The full tool definition is Romik's contract format. Only the annotations this half
depends on are defined here, so both halves share one spelling.
"""

from enum import StrEnum


class Behavior(StrEnum):
    """What a tool does to upstream state. Drives idempotency (§7) and scope checks (§9)."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"

    @property
    def changes_state(self) -> bool:
        return self is not Behavior.READ_ONLY


class Sensitivity(StrEnum):
    """Per-field ``x-sensitivity`` annotation used by redaction (§8.4).

    A field with no annotation is treated as SECRET: redaction is allow-list based.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"  # noqa: S105 - an enum label, not a credential

    @property
    def may_appear_unmasked(self) -> bool:
        return self in {Sensitivity.PUBLIC, Sensitivity.INTERNAL}


UNANNOTATED_SENSITIVITY = Sensitivity.SECRET
