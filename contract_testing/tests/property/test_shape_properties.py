"""Canonical-order and hashing laws for Shape (brief §5.2, §12)."""

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapter_kernel.shape import (
    FieldShape,
    FieldType,
    OperationShape,
    Provenance,
    ProvenanceKind,
    Shape,
    SourceKind,
)

pytestmark = pytest.mark.property

_names = st.text(alphabet="abcdefghij._[]", min_size=1, max_size=8)


@st.composite
def field_shapes(draw: st.DrawFn, path: str) -> FieldShape:
    type_ = draw(st.sampled_from(FieldType))
    enum_values = (
        draw(st.frozensets(_names, min_size=1, max_size=4)) if type_ is FieldType.ENUM else None
    )
    return FieldShape(
        path=path,
        type=type_,
        required=draw(st.booleans()),
        nullable=draw(st.booleans()),
        enum_values=enum_values,
        format=draw(st.none() | _names),
    )


@st.composite
def operations(draw: st.DrawFn, name: str) -> OperationShape:
    in_paths = draw(st.lists(_names, unique=True, max_size=4))
    out_paths = draw(st.lists(_names, unique=True, max_size=4))
    return OperationShape(
        name=name,
        inputs=tuple(draw(field_shapes(p)) for p in in_paths),
        outputs=tuple(draw(field_shapes(p)) for p in out_paths),
        errors=draw(st.frozensets(_names, max_size=3)),
    )


@st.composite
def operation_lists(draw: st.DrawFn) -> list[OperationShape]:
    names = draw(st.lists(_names, unique=True, max_size=4))
    return [draw(operations(n)) for n in names]


def _shape(ops: list[OperationShape]) -> Shape:
    return Shape(
        source_id="synthetic.source",
        source_kind=SourceKind.QUEUE,
        source_version="1",
        operations=tuple(ops),
        provenance=Provenance(
            kind=ProvenanceKind.DECLARED,
            sample_count=None,
            extractor="synthetic",
            extractor_version="0",
            extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


@given(ops=operation_lists(), data=st.data())
def test_hash_and_serialization_are_order_independent(
    ops: list[OperationShape], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(ops))
    a, b = _shape(ops), _shape(list(shuffled))
    assert a == b
    assert a.model_dump_json() == b.model_dump_json()
    assert a.content_hash() == b.content_hash()


@given(ops=operation_lists())
def test_json_round_trip_is_identity(ops: list[OperationShape]) -> None:
    shape = _shape(ops)
    restored = Shape.model_validate_json(shape.model_dump_json())
    assert restored == shape
    assert restored.content_hash() == shape.content_hash()


@given(a=operation_lists(), b=operation_lists())
def test_equal_structure_iff_equal_hash(a: list[OperationShape], b: list[OperationShape]) -> None:
    same_structure = _shape(a).operations == _shape(b).operations
    assert same_structure == (_shape(a).content_hash() == _shape(b).content_hash())
