from datetime import UTC, datetime, timedelta

import pytest

from adapter_kernel.jsontypes import JsonValue
from adapter_kernel.shape import FieldType, ProvenanceKind, Shape
from adapter_verify.contract_ci.domain.differ import diff
from adapter_verify.contract_ci.domain.extract import (
    ColumnSpec,
    EvidenceThreshold,
    ExtractionError,
    Sample,
    extract_csv,
    extract_layout,
    extract_observed,
)

pytestmark = pytest.mark.unit

_AT = datetime(2026, 9, 1, tzinfo=UTC)
LOW = EvidenceThreshold(min_samples=2, min_days=1)


def observed(
    bodies: list[JsonValue],
    *,
    threshold: EvidenceThreshold = LOW,
    days: int = 2,
    enums: tuple[str, ...] = (),
) -> Shape:
    step = timedelta(days=days) / max(len(bodies) - 1, 1)
    samples = [Sample(captured_at=_AT + step * i, body=b) for i, b in enumerate(bodies)]
    return extract_observed(
        samples,
        source_id="s",
        version="v1",
        operation="get",
        enum_paths=enums,
        threshold=threshold,
        extracted_at=_AT,
    )


def fields(shape: Shape) -> dict[str, tuple[FieldType, bool, bool, str | None]]:
    return {f.path: (f.type, f.required, f.nullable, f.format) for f in shape.operations[0].outputs}


def test_observed_types_presence_nesting_and_dates() -> None:
    shape = observed(
        [
            {
                "id": "A",
                "qty": 1,
                "price": 1.5,
                "ok": True,
                "c": {"id": "X"},
                "at": "09/01/2026 10:00:00",
                "note": None,
            },
            {
                "id": "B",
                "qty": 2,
                "price": 2,
                "ok": False,
                "c": {"id": "Y"},
                "at": "09/02/2026 10:00:00",
            },
        ]
    )
    assert fields(shape) == {
        "id": (FieldType.STRING, True, False, None),
        "qty": (FieldType.INTEGER, True, False, None),
        "price": (FieldType.NUMBER, True, False, None),
        "ok": (FieldType.BOOLEAN, True, False, None),
        "c": (FieldType.OBJECT, True, False, None),
        "c.id": (FieldType.STRING, True, False, None),
        "at": (FieldType.DATETIME, True, False, "US"),
        "note": (FieldType.NULL, False, True, None),
    }


def test_arrays_and_item_fields() -> None:
    shape = observed([{"items": [{"sku": "1", "n": 1}, {"sku": "2"}]}, {"items": [{"sku": "3"}]}])
    got = fields(shape)
    assert got["items"][0] is FieldType.ARRAY
    assert got["items[].sku"][:2] == (FieldType.STRING, True)
    assert got["items[].n"][:2] == (FieldType.INTEGER, False)


def test_enums_are_collected_for_declared_paths() -> None:
    shape = observed([{"s": "OPEN"}, {"s": "CLOSED"}], enums=("s",))
    (field,) = shape.operations[0].outputs
    assert field.type is FieldType.ENUM
    assert field.enum_values == frozenset({"OPEN", "CLOSED"})


@pytest.mark.parametrize(
    ("bodies", "enums", "match"),
    [
        ([{"x": "a"}, {"x": 1}], (), "conflicting types"),
        ([{"d": "09/01/2026 10:00:00"}, {"d": "2026-09-01T10:00:00Z"}], (), "mixed formats"),
        ([{"s": 1}], ("s",), "non-string"),
        ([[1, 2]], (), "JSON object"),
        ([], (), "no samples"),
    ],
)
def test_ambiguity_fails_closed(
    bodies: list[JsonValue], enums: tuple[str, ...], match: str
) -> None:
    with pytest.raises(ExtractionError, match=match):
        observed(bodies, enums=enums)


def test_provenance_strength_needs_count_and_span() -> None:
    strong = EvidenceThreshold(min_samples=3, min_days=7)
    assert (
        observed([{"a": 1}] * 3, threshold=strong, days=8).provenance.kind
        is ProvenanceKind.OBSERVED
    )
    assert (
        observed([{"a": 1}] * 3, threshold=strong, days=2).provenance.kind
        is ProvenanceKind.INFERRED
    )
    assert (
        observed([{"a": 1}] * 2, threshold=strong, days=8).provenance.kind
        is ProvenanceKind.INFERRED
    )


LAYOUT = (
    ColumnSpec(name="item_id", type=FieldType.STRING),
    ColumnSpec(name="status", type=FieldType.ENUM, enum_values=("AVAIL", "RSVD")),
    ColumnSpec(name="qty", type=FieldType.INTEGER),
)


def csv_shape(*texts: str, delimiter: str = ",") -> Shape:
    return extract_csv(
        [(_AT, t) for t in texts],
        layout=LAYOUT,
        delimiter=delimiter,
        source_id="f",
        version="v1",
        operation="feed",
        threshold=LOW,
        extracted_at=_AT,
    )


def test_csv_matches_declared_layout_and_ignores_column_order() -> None:
    declared = extract_layout(
        LAYOUT, source_id="f", version="v1", operation="feed", extracted_at=_AT
    )
    in_order = csv_shape("item_id,status,qty\nI1,AVAIL,3\nI2,RSVD,0\n")
    reordered = csv_shape("qty,item_id,status\n3,I1,AVAIL\n0,I2,RSVD\n")
    assert diff(declared, in_order) == []
    assert diff(in_order, reordered) == []


def test_csv_delimiter_change_is_unparseable() -> None:
    with pytest.raises(ExtractionError, match="delimiter"):
        csv_shape("item_id;status;qty\nI1;AVAIL;3\n")


def test_csv_empty_cells_are_null_and_missing_rows_error() -> None:
    shape = csv_shape("item_id,status,qty\nI1,AVAIL,\nI2,RSVD,4\n")
    assert fields(shape)["qty"][2] is True
    with pytest.raises(ExtractionError, match="no rows"):
        csv_shape("item_id,status,qty\n")
