import pytest

from adapter_kernel.classification import Classification, worst
from adapter_kernel.tooldef import UNANNOTATED_SENSITIVITY, Behavior, Sensitivity

pytestmark = pytest.mark.unit


def test_worst_of_nothing_is_compatible() -> None:
    assert worst([]) is Classification.COMPATIBLE


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (
            [Classification.COMPATIBLE, Classification.REVIEW_REQUIRED],
            Classification.REVIEW_REQUIRED,
        ),
        ([Classification.BREAKING, Classification.REVIEW_REQUIRED], Classification.BREAKING),
        ([Classification.BREAKING, Classification.UNKNOWN], Classification.UNKNOWN),
        ([Classification.UNKNOWN, Classification.COMPATIBLE], Classification.UNKNOWN),
    ],
)
def test_worst_picks_most_severe(inputs: list[Classification], expected: Classification) -> None:
    assert worst(inputs) is expected


def test_only_read_only_leaves_state_unchanged() -> None:
    assert not Behavior.READ_ONLY.changes_state
    assert Behavior.MUTATING.changes_state
    assert Behavior.DESTRUCTIVE.changes_state


def test_unannotated_fields_are_never_shown_unmasked() -> None:
    assert not UNANNOTATED_SENSITIVITY.may_appear_unmasked


def test_only_public_and_internal_appear_unmasked() -> None:
    shown = {s for s in Sensitivity if s.may_appear_unmasked}
    assert shown == {Sensitivity.PUBLIC, Sensitivity.INTERNAL}
