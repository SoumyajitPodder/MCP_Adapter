"""Redaction never leaks (brief §8.4, §8.10): every output leaf whose path is not annotated
public/internal is a mask, whatever the document and annotations."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapter_kernel.jsontypes import JsonValue
from adapter_kernel.tooldef import Sensitivity
from adapter_verify.observability.domain.redaction import (
    UNKNOWN_KEYS_FIELD,
    RedactionPolicy,
    child_path,
    item_path,
)

pytestmark = pytest.mark.property

SENTINEL = "SENTINEL-7f3a"
_keys = st.sampled_from(["a", "b", "c", "id", "name", "a.b", "a[]", "b[].c"])


def _documents() -> st.SearchStrategy[JsonValue]:
    leaves = st.one_of(st.just(SENTINEL), st.integers(), st.booleans(), st.none())
    return st.recursive(
        leaves,
        lambda inner: st.lists(inner, max_size=3) | st.dictionaries(_keys, inner, max_size=3),
        max_leaves=15,
    )


def _leaves(value: JsonValue, path: str = "") -> list[tuple[str, JsonValue]]:
    if isinstance(value, dict):
        return [lf for k, v in value.items() for lf in _leaves(v, child_path(path, k))]
    if isinstance(value, list):
        return [lf for v in value for lf in _leaves(v, item_path(path))]
    return [(path, value)]


@given(doc=_documents(), data=st.data())
def test_only_shown_paths_keep_their_values(doc: JsonValue, data: st.DataObject) -> None:
    paths = sorted({p for p, _ in _leaves(doc)})
    levels = data.draw(st.lists(st.sampled_from(Sensitivity), min_size=len(paths)))
    sensitivity = dict(zip(paths, levels, strict=False))
    shown = {p for p, s in sensitivity.items() if s.may_appear_unmasked}

    for path, value in _leaves(RedactionPolicy(sensitivity).redact(doc)):
        if path in shown or path.endswith(UNKNOWN_KEYS_FIELD):
            continue
        assert isinstance(value, str)
        assert value.startswith("<redacted:")


def _aliased_values(value: JsonValue, *, aliased: bool = False) -> list[JsonValue]:
    if isinstance(value, dict):
        return [
            leaf
            for k, v in value.items()
            for leaf in _aliased_values(v, aliased=aliased or any(c in k for c in ".[]"))
        ]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _aliased_values(v, aliased=aliased)]
    return [value] if aliased else []


@given(doc=_documents(), level=st.sampled_from(Sensitivity))
def test_keys_with_path_separators_never_reach_the_output(
    doc: JsonValue, level: Sensitivity
) -> None:
    """Even when every path in the document is annotated, a value under a key containing
    ``.``/``[]`` is never emitted: such keys are dropped, not matched against the schema."""
    sensitivity = {p: level for p, _ in _leaves(doc)}
    out = RedactionPolicy(sensitivity).redact(doc)
    assert _aliased_values(out) == []


@given(doc=_documents())
def test_nothing_is_shown_without_annotations(doc: JsonValue) -> None:
    assert SENTINEL not in json.dumps(RedactionPolicy({}).redact(doc))
