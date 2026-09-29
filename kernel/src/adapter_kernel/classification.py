"""Change classification vocabulary shared by runtime drift (§3) and contract CI (§5)."""

from collections.abc import Iterable
from enum import StrEnum


class Classification(StrEnum):
    """How safe a detected change is for existing consumers."""

    COMPATIBLE = "COMPATIBLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BREAKING = "BREAKING"
    UNKNOWN = "UNKNOWN"


# UNKNOWN ranks above BREAKING: an unknown impact is never gated more leniently than a known break.
_SEVERITY: dict[Classification, int] = {
    Classification.COMPATIBLE: 0,
    Classification.REVIEW_REQUIRED: 1,
    Classification.BREAKING: 2,
    Classification.UNKNOWN: 3,
}


def worst(classifications: Iterable[Classification]) -> Classification:
    """Return the most severe classification. No findings means COMPATIBLE."""
    return max(classifications, key=_SEVERITY.__getitem__, default=Classification.COMPATIBLE)
