from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from adapter_kernel.shape import (
    FieldShape,
    FieldType,
    OperationShape,
    Provenance,
    ProvenanceKind,
    Shape,
    SourceKind,
)

pytestmark = pytest.mark.unit

_WHEN = datetime(2026, 1, 1, tzinfo=UTC)


def _declared() -> Provenance:
    return Provenance(
        kind=ProvenanceKind.DECLARED,
        sample_count=None,
        extractor="synthetic",
        extractor_version="0",
        extracted_at=_WHEN,
    )


def _field(path: str, type_: FieldType = FieldType.STRING) -> FieldShape:
    return FieldShape(path=path, type=type_, required=True, nullable=False)


def _shape(*ops: OperationShape, version: str = "1") -> Shape:
    return Shape(
        source_id="synthetic.source",
        source_kind=SourceKind.OPENAPI,
        source_version=version,
        operations=ops,
        provenance=_declared(),
    )


def _op(name: str, *outputs: FieldShape, errors: frozenset[str] = frozenset()) -> OperationShape:
    return OperationShape(name=name, inputs=(), outputs=outputs, errors=errors)


def test_fields_and_operations_are_stored_sorted() -> None:
    shape = _shape(_op("b", _field("z"), _field("a")), _op("a"))
    assert [o.name for o in shape.operations] == ["a", "b"]
    assert [f.path for f in shape.operations[1].outputs] == ["a", "z"]


def test_duplicate_paths_and_operations_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate field path"):
        _op("a", _field("x"), _field("x"))
    with pytest.raises(ValidationError, match="duplicate operation name"):
        _shape(_op("a"), _op("a"))


def test_enum_values_required_exactly_for_enums() -> None:
    with pytest.raises(ValidationError, match="enum_values"):
        FieldShape(path="s", type=FieldType.ENUM, required=True, nullable=False)
    with pytest.raises(ValidationError, match="at least one"):
        FieldShape(
            path="s", type=FieldType.ENUM, required=True, nullable=False, enum_values=frozenset()
        )
    with pytest.raises(ValidationError, match="enum_values"):
        FieldShape(
            path="s",
            type=FieldType.STRING,
            required=True,
            nullable=False,
            enum_values=frozenset({"A"}),
        )


def test_sample_count_matches_provenance_kind() -> None:
    with pytest.raises(ValidationError, match="sample_count"):
        Provenance(
            kind=ProvenanceKind.OBSERVED,
            sample_count=None,
            extractor="x",
            extractor_version="0",
            extracted_at=_WHEN,
        )
    with pytest.raises(ValidationError, match="sample_count"):
        Provenance(
            kind=ProvenanceKind.DECLARED,
            sample_count=5,
            extractor="x",
            extractor_version="0",
            extracted_at=_WHEN,
        )


def test_naive_extraction_time_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            kind=ProvenanceKind.DECLARED,
            sample_count=None,
            extractor="x",
            extractor_version="0",
            extracted_at=datetime(2026, 1, 1),  # noqa: DTZ001 - deliberately naive
        )


def test_hash_ignores_version_and_provenance() -> None:
    assert (
        _shape(_op("a"), version="1").content_hash() == _shape(_op("a"), version="2").content_hash()
    )


def test_hash_changes_with_structure() -> None:
    base = _shape(_op("a", _field("x")))
    assert base.content_hash() != _shape(_op("a", _field("x", FieldType.INTEGER))).content_hash()
    assert (
        base.content_hash() != _shape(_op("a", _field("x"), errors=frozenset({"E"}))).content_hash()
    )


def test_json_round_trip_preserves_equality() -> None:
    shape = _shape(
        _op(
            "a",
            FieldShape(
                path="status",
                type=FieldType.ENUM,
                required=True,
                nullable=False,
                enum_values=frozenset({"OPEN", "CLOSED"}),
            ),
            errors=frozenset({"NOT_FOUND", "TIMEOUT"}),
        )
    )
    assert Shape.model_validate_json(shape.model_dump_json()) == shape
