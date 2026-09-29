from datetime import UTC, datetime

import pytest
from hypothesis import given

from adapter_kernel.classification import Classification
from adapter_kernel.shape import (
    FieldShape,
    FieldType,
    OperationShape,
    Provenance,
    ProvenanceKind,
    Shape,
    SourceKind,
)
from adapter_verify.contract_ci.adapters.files import load_rulebook
from adapter_verify.contract_ci.domain.differ import classify, diff
from adapter_verify.contract_ci.domain.rules import CheckKind
from tests.property.test_shape_properties import operation_lists

pytestmark = pytest.mark.unit

BOOK = load_rulebook()
_AT = datetime(2026, 9, 1, tzinfo=UTC)


def f(
    path: str,
    t: FieldType = FieldType.STRING,
    *,
    req: bool = True,
    null: bool = False,
    enum: set[str] | None = None,
    fmt: str | None = None,
) -> FieldShape:
    return FieldShape(
        path=path,
        type=t,
        required=req,
        nullable=null,
        enum_values=frozenset(enum) if enum else None,
        format=fmt,
    )


def shape(*ops: OperationShape, inferred: bool = False) -> Shape:
    return Shape(
        source_id="s",
        source_kind=SourceKind.OBSERVED,
        source_version="1",
        operations=ops,
        provenance=Provenance(
            kind=ProvenanceKind.INFERRED if inferred else ProvenanceKind.DECLARED,
            sample_count=3 if inferred else None,
            extractor="t",
            extractor_version="1",
            extracted_at=_AT,
        ),
    )


def op(
    name: str = "get",
    *,
    inputs: tuple[FieldShape, ...] = (),
    outputs: tuple[FieldShape, ...] = (),
    errors: frozenset[str] = frozenset(),
) -> OperationShape:
    return OperationShape(name=name, inputs=inputs, outputs=outputs, errors=errors)


ENUM = f("state", FieldType.ENUM, enum={"A", "B"})

# rule → (base, rev). Each case changes exactly one thing.
FIRES: dict[str, tuple[Shape, Shape]] = {
    "OPERATION_REMOVED": (shape(op("a"), op("b")), shape(op("a"))),
    "OPERATION_ADDED": (shape(op("a")), shape(op("a"), op("b"))),
    "OUTPUT_FIELD_REMOVED": (
        shape(op(outputs=(f("x"), f("qty", FieldType.INTEGER)))),
        shape(op(outputs=(f("x"),))),
    ),
    "OUTPUT_FIELD_ADDED": (
        shape(op(outputs=(f("x"),))),
        shape(op(outputs=(f("x"), f("zzz", FieldType.INTEGER)))),
    ),
    "INPUT_REQUIRED_FIELD_ADDED": (shape(op()), shape(op(inputs=(f("id"),)))),
    "INPUT_OPTIONAL_FIELD_ADDED": (shape(op()), shape(op(inputs=(f("id", req=False),)))),
    "INPUT_FIELD_REMOVED": (shape(op(inputs=(f("id"),))), shape(op())),
    "TYPE_CHANGED": (shape(op(outputs=(f("x"),))), shape(op(outputs=(f("x", FieldType.INTEGER),)))),
    "OUTPUT_BECAME_OPTIONAL": (
        shape(op(outputs=(f("x"),))),
        shape(op(outputs=(f("x", req=False),))),
    ),
    "OUTPUT_BECAME_REQUIRED": (
        shape(op(outputs=(f("x", req=False),))),
        shape(op(outputs=(f("x"),))),
    ),
    "INPUT_BECAME_REQUIRED": (shape(op(inputs=(f("x", req=False),))), shape(op(inputs=(f("x"),)))),
    "INPUT_BECAME_OPTIONAL": (shape(op(inputs=(f("x"),))), shape(op(inputs=(f("x", req=False),)))),
    "OUTPUT_BECAME_NULLABLE": (
        shape(op(outputs=(f("x"),))),
        shape(op(outputs=(f("x", null=True),))),
    ),
    "OUTPUT_BECAME_NON_NULLABLE": (
        shape(op(outputs=(f("x", null=True),))),
        shape(op(outputs=(f("x"),))),
    ),
    "INPUT_BECAME_NULLABLE": (shape(op(inputs=(f("x"),))), shape(op(inputs=(f("x", null=True),)))),
    "INPUT_BECAME_NON_NULLABLE": (
        shape(op(inputs=(f("x", null=True),))),
        shape(op(inputs=(f("x"),))),
    ),
    "OUTPUT_ENUM_VALUE_ADDED": (
        shape(op(outputs=(ENUM,))),
        shape(op(outputs=(f("state", FieldType.ENUM, enum={"A", "B", "C"}),))),
    ),
    "OUTPUT_ENUM_VALUE_REMOVED": (
        shape(op(outputs=(ENUM,))),
        shape(op(outputs=(f("state", FieldType.ENUM, enum={"A"}),))),
    ),
    "INPUT_ENUM_VALUE_ADDED": (
        shape(op(inputs=(ENUM,))),
        shape(op(inputs=(f("state", FieldType.ENUM, enum={"A", "B", "C"}),))),
    ),
    "INPUT_ENUM_VALUE_REMOVED": (
        shape(op(inputs=(ENUM,))),
        shape(op(inputs=(f("state", FieldType.ENUM, enum={"A"}),))),
    ),
    "FORMAT_CHANGED": (
        shape(op(outputs=(f("d", FieldType.DATETIME, fmt="US"),))),
        shape(op(outputs=(f("d", FieldType.DATETIME, fmt="ISO"),))),
    ),
    "ERROR_ADDED": (shape(op()), shape(op(errors=frozenset({"E"})))),
    "ERROR_REMOVED": (shape(op(errors=frozenset({"E"}))), shape(op())),
    "FIELD_RENAMED_SUSPECTED": (
        shape(op(outputs=(f("orderState"),))),
        shape(op(outputs=(f("order_state"),))),
    ),
}
EMITTED_ONLY_WITH_CONTEXT = {"FIELD_RENAMED_KNOWN", "SOURCE_UNPARSEABLE", "UNCLASSIFIED_CHANGE"}


def test_every_rule_has_a_firing_case() -> None:
    assert set(FIRES) | EMITTED_ONLY_WITH_CONTEXT == {r.id for r in BOOK.rules}


@pytest.mark.parametrize("rule", sorted(FIRES))
def test_rule_fires_alone(rule: str) -> None:
    base, rev = FIRES[rule]
    assert {c.rule_id for c in diff(base, rev)} == {rule}


@pytest.mark.parametrize("rule", sorted(FIRES))
def test_rule_does_not_fire_without_the_change(rule: str) -> None:
    base, _ = FIRES[rule]
    assert diff(base, base) == []


def test_known_alias_beats_suspicion_and_unrelated_names_do_not_pair() -> None:
    base, rev = (
        shape(op(outputs=(f("quantity_available", FieldType.INTEGER),))),
        shape(op(outputs=(f("qty_avail", FieldType.INTEGER),))),
    )
    known = diff(base, rev, aliases={"get": {"quantity_available": "qty_avail"}})
    assert [c.rule_id for c in known] == ["FIELD_RENAMED_KNOWN"]
    unrelated = diff(shape(op(outputs=(f("status"),))), shape(op(outputs=(f("warehouse"),))))
    assert {c.rule_id for c in unrelated} == {"OUTPUT_FIELD_REMOVED", "OUTPUT_FIELD_ADDED"}


def test_renamed_pairs_still_report_attribute_changes() -> None:
    changes = diff(
        shape(op(outputs=(f("orderState"),))), shape(op(outputs=(f("order_state", null=True),)))
    )
    assert {c.rule_id for c in changes} == {"FIELD_RENAMED_SUSPECTED", "OUTPUT_BECAME_NULLABLE"}


def test_different_sources_cannot_be_diffed() -> None:
    other = shape(op()).model_copy(update={"source_id": "other"})
    with pytest.raises(ValueError, match="same source"):
        diff(shape(op()), other)


@given(ops=operation_lists())
def test_diff_with_itself_is_empty(ops: list[OperationShape]) -> None:
    s = shape(*ops)
    assert diff(s, s) == []


@given(a=operation_lists(), b=operation_lists())
def test_empty_diff_iff_equal_hash(a: list[OperationShape], b: list[OperationShape]) -> None:
    x, y = shape(*a), shape(*b)
    assert (diff(x, y) == []) == (x.content_hash() == y.content_hash())


def test_classification_adjustments() -> None:
    base, rev = FIRES["OUTPUT_FIELD_ADDED"]
    (plain,) = classify(diff(base, rev), BOOK, CheckKind.UPSTREAM, inferred=False)
    (weak,) = classify(diff(base, rev), BOOK, CheckKind.UPSTREAM, inferred=True)
    assert plain.classification is Classification.COMPATIBLE
    assert weak.classification is Classification.REVIEW_REQUIRED
    assert weak.note is not None

    base, rev = FIRES["OUTPUT_ENUM_VALUE_ADDED"]
    (patch,) = classify(diff(base, rev), BOOK, CheckKind.CANONICAL, inferred=False)
    (minor,) = classify(diff(base, rev), BOOK, CheckKind.CANONICAL, inferred=False, minor_bump=True)
    assert patch.classification is Classification.REVIEW_REQUIRED
    assert minor.classification is Classification.COMPATIBLE
