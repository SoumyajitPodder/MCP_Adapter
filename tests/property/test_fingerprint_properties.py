"""§7.3: key order and whitespace never change a fingerprint; a different value always does."""

import json
from typing import cast

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from adapter_kernel.jsontypes import JsonObject, JsonValue
from adapter_verify.idempotency.domain.keys import fingerprint

pytestmark = pytest.mark.property

_SAFE_INT = 2**53 - 1
scalars = (
    st.none()
    | st.booleans()
    | st.integers(-_SAFE_INT, _SAFE_INT)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=8)
)
values = st.recursive(
    scalars,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=6), inner, max_size=4)
    ),
    max_leaves=12,
)
objects = st.dictionaries(st.text(max_size=6), values, max_size=5)


def _reordered(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {k: _reordered(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reordered(v) for v in value]
    return value


def _json_equal(a: JsonValue, b: JsonValue) -> bool:
    """JSON value equality: one number type (1 == 1.0, -0.0 == 0.0), booleans are not numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


@given(objects)
def test_key_order_and_whitespace_do_not_matter(obj: JsonObject) -> None:
    reparsed = cast(JsonObject, json.loads(json.dumps(obj, indent=3)))
    assert fingerprint(cast(JsonObject, _reordered(obj))) == fingerprint(obj)
    assert fingerprint(reparsed) == fingerprint(obj)


@given(objects, objects)
def test_different_arguments_have_different_fingerprints(a: JsonObject, b: JsonObject) -> None:
    assume(not _json_equal(a, b))
    assert fingerprint(a) != fingerprint(b)


@given(objects)
def test_equal_json_values_share_a_fingerprint(obj: JsonObject) -> None:
    floats = cast(JsonObject, json.loads(json.dumps(obj), parse_int=float))
    assume(_json_equal(obj, floats))
    assert fingerprint(floats) == fingerprint(obj)
