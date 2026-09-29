import pytest

from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.tooldef import Sensitivity
from adapter_verify.observability.domain.redaction import (
    UNANNOTATED_MASK,
    UNKNOWN_KEYS_FIELD,
    RedactionPolicy,
    sensitivity_map,
)

pytestmark = pytest.mark.unit

SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "x-sensitivity": "internal"},
        "status": {"type": "string", "x-sensitivity": "public"},
        "customer": {
            "type": "object",
            "x-sensitivity": "public",
            "properties": {
                "name": {"type": "string", "x-sensitivity": "pii"},
                "email": {"type": "string"},
            },
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "x-sensitivity": "public"},
                    "note": {"type": "string"},
                },
            },
        },
        "api_token": {"type": "string", "x-sensitivity": "secret"},
    },
}


def test_sensitivity_map_collects_nested_paths() -> None:
    assert sensitivity_map(SCHEMA) == {
        "order_id": Sensitivity.INTERNAL,
        "status": Sensitivity.PUBLIC,
        "customer": Sensitivity.PUBLIC,
        "customer.name": Sensitivity.PII,
        "items[].sku": Sensitivity.PUBLIC,
        "api_token": Sensitivity.SECRET,
    }


@pytest.mark.parametrize("bad", ["confidential", 3, None])
def test_invalid_annotation_is_rejected(bad: str | int | None) -> None:
    schema: JsonObject = {"properties": {"a": {"x-sensitivity": bad}}}
    if bad is None:
        assert sensitivity_map(schema) == {}
        return
    with pytest.raises(ValueError, match="invalid x-sensitivity at a"):
        sensitivity_map(schema)


def test_redaction_is_allow_list_based() -> None:
    policy = RedactionPolicy.from_schema(SCHEMA)
    payload: JsonObject = {
        "order_id": "88213",
        "status": "IN_PROGRESS",
        "customer": {"name": "Synthetic Person", "email": "synthetic@example.invalid"},
        "items": [{"sku": "SKU-1", "note": "leave at door"}],
        "api_token": "sentinel-token",
    }
    assert policy.redact(payload) == {
        "order_id": "88213",
        "status": "IN_PROGRESS",
        "customer": {"name": "<redacted:pii>", "email": UNANNOTATED_MASK},
        "items": [{"sku": "SKU-1", "note": UNANNOTATED_MASK}],
        "api_token": "<redacted:secret>",
    }


def test_undeclared_keys_are_dropped_even_next_to_declared_ones() -> None:
    policy = RedactionPolicy.from_schema(SCHEMA)
    redacted = policy.redact({"status": "OK", "customer": {"ssn": "x", "email": "y"}})
    assert redacted == {
        "status": "OK",
        "customer": {"email": UNANNOTATED_MASK, UNKNOWN_KEYS_FIELD: 1},
    }


def test_unknown_keys_are_dropped_and_counted() -> None:
    policy = RedactionPolicy({"status": Sensitivity.PUBLIC})
    redacted = policy.redact({"status": "OK", "ACCT-5550001": {"balance": 1}, "x": 2})
    assert redacted == {"status": "OK", UNKNOWN_KEYS_FIELD: 2}


def test_container_annotation_does_not_unmask_children() -> None:
    policy = RedactionPolicy({"customer": Sensitivity.PUBLIC, "customer.name": Sensitivity.PII})
    assert policy.redact({"customer": {"name": "n"}}) == {"customer": {"name": "<redacted:pii>"}}


def test_object_where_leaf_expected_is_emptied() -> None:
    policy = RedactionPolicy({"status": Sensitivity.PUBLIC})
    assert policy.redact({"status": {"hidden": "x"}}) == {"status": {UNKNOWN_KEYS_FIELD: 1}}


def test_empty_policy_masks_everything() -> None:
    assert RedactionPolicy({}).redact("scalar") == UNANNOTATED_MASK
    assert RedactionPolicy({}).redact({"a": 1}) == {UNKNOWN_KEYS_FIELD: 1}
    assert RedactionPolicy({}).redact([1, 2]) == [UNANNOTATED_MASK, UNANNOTATED_MASK]


def test_dotted_data_key_does_not_alias_a_nested_public_path() -> None:
    policy = RedactionPolicy.from_schema(
        {"properties": {"customer": {"properties": {"tier": {"x-sensitivity": "public"}}}}}
    )
    assert policy.redact({"customer.tier": "sentinel-secret"}) == {UNKNOWN_KEYS_FIELD: 1}
    assert policy.redact({"customer": {"tier": "gold"}}) == {"customer": {"tier": "gold"}}


@pytest.mark.parametrize("key", ["items[]", "items[].sku", "items[0]", "a]b", "c[d"])
def test_bracketed_data_keys_are_dropped_and_counted(key: str) -> None:
    policy = RedactionPolicy({"status": Sensitivity.PUBLIC, key: Sensitivity.PUBLIC})
    assert policy.redact({key: "sentinel", "status": "OK"}) == {
        "status": "OK",
        UNKNOWN_KEYS_FIELD: 1,
    }


@pytest.mark.parametrize("name", ["customer.tier", "items[]", "a[0]", "b]"])
def test_schema_property_names_with_path_separators_are_rejected(name: str) -> None:
    schema: JsonObject = {
        "properties": {"outer": {"properties": {name: {"x-sensitivity": "public"}}}}
    }
    with pytest.raises(ValueError, match="at outer contains"):
        sensitivity_map(schema)
    with pytest.raises(ValueError, match="at outer contains"):
        RedactionPolicy.from_schema(schema)
