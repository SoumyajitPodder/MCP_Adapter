import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from adapter_kernel.errors import ErrorCode
from adapter_kernel.tooldef import Behavior
from adapter_verify.access.domain.evaluate import (
    RULE_NO_POLICY,
    RULE_SCOPE_READ_ONLY,
    RULE_TOOL_NOT_GRANTED,
    RULE_VERSION_OUT_OF_RANGE,
    evaluate,
    visible_tools,
)
from adapter_verify.access.domain.policy import CatalogEntry, Grant, PolicySet, Scope, ToolCatalog
from adapter_verify.access.domain.semver import Version
from tests.unit.access.support import CATALOG, READER, WRITER, policy

pytestmark = pytest.mark.unit

POLICIES = PolicySet([READER, WRITER])
V1 = Version.parse("1.2.0")


@pytest.mark.parametrize(
    ("agent", "tool", "version", "behavior", "rule", "code"),
    [
        (
            "nobody",
            "order.get",
            "1.2.0",
            Behavior.READ_ONLY,
            RULE_NO_POLICY,
            ErrorCode.NOT_AUTHORIZED,
        ),
        (
            "reader",
            "service.get",
            "1.0.0",
            Behavior.READ_ONLY,
            RULE_TOOL_NOT_GRANTED,
            ErrorCode.NOT_AUTHORIZED,
        ),
        (
            "reader",
            "order.get",
            "2.0.0",
            Behavior.READ_ONLY,
            RULE_VERSION_OUT_OF_RANGE,
            ErrorCode.VERSION_NOT_PERMITTED,
        ),
        (
            "reader",
            "order.get",
            "1.3.0-beta",
            Behavior.READ_ONLY,
            RULE_VERSION_OUT_OF_RANGE,
            ErrorCode.VERSION_NOT_PERMITTED,
        ),
        (
            "reader",
            "order.get",
            "1.2.0",
            Behavior.MUTATING,
            RULE_SCOPE_READ_ONLY,
            ErrorCode.SCOPE_VIOLATION,
        ),
    ],
)
def test_denials_follow_the_decision_table(
    agent: str, tool: str, version: str, behavior: Behavior, rule: str, code: ErrorCode
) -> None:
    d = evaluate(POLICIES, agent, tool, Version.parse(version), behavior)
    assert not d.allowed
    assert (d.rule_id, d.error_code) == (rule, code)
    assert d.policy_digest == POLICIES.digest


def test_allow_names_the_granting_rule() -> None:
    d = evaluate(POLICIES, "writer", "order.cancel", Version.parse("1.0.0"), Behavior.MUTATING)
    assert d.allowed
    assert d.error_code is None
    assert d.rule_id == "ACCESS_GRANTED:writer/order.cancel/1"


def test_major_bump_never_inherits_access() -> None:
    d = evaluate(POLICIES, "reader", "order.get", Version.parse("2.0.0"), Behavior.READ_ONLY)
    assert d.error_code is ErrorCode.VERSION_NOT_PERMITTED


def test_digest_is_order_independent_and_content_sensitive() -> None:
    assert PolicySet([WRITER, READER]).digest == POLICIES.digest
    assert PolicySet([READER]).digest != POLICIES.digest


def test_duplicate_agents_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate policy"):
        PolicySet([READER, READER])


def test_policy_models_validate_inputs() -> None:
    with pytest.raises(ValidationError):
        Grant(tool="order.get", versions="~1.0")
    with pytest.raises(ValidationError):
        policy(agent="has space")


def test_catalog_requires_one_default_and_unique_entries() -> None:
    entry = CatalogEntry(tool="a.b", version="1.0.0", behavior=Behavior.READ_ONLY)
    with pytest.raises(ValidationError, match="exactly one default"):
        ToolCatalog(tools=(entry,))
    with pytest.raises(ValidationError, match="duplicate catalog entry"):
        ToolCatalog(tools=(entry.model_copy(update={"default": True}), entry))
    with pytest.raises(ValidationError):
        CatalogEntry(tool="a.b", version="1.0", behavior=Behavior.READ_ONLY)


def test_catalog_resolves_default_or_exact() -> None:
    assert CATALOG.resolve("order.get", None) == CATALOG.tools[0]
    assert CATALOG.resolve("order.get", "2.0.0") == CATALOG.tools[1]
    assert CATALOG.resolve("order.get", "9.9.9") is None
    assert CATALOG.resolve("nope", None) is None


_grants = st.lists(
    st.tuples(
        st.sampled_from(["order.get", "service.get", "order.cancel"]),
        st.sampled_from([">=1.0.0,<2.0.0", ">=2.0.0,<3.0.0", "==1.2.0", ">=1.0.0,<1.1.0"]),
    ),
    max_size=4,
)


@given(grants=_grants, scope=st.sampled_from(Scope))
def test_listing_never_disagrees_with_calling(grants: list[tuple[str, str]], scope: Scope) -> None:
    """§9.5: hiding is never looser or tighter than blocking."""
    policies = PolicySet([policy(agent="a", scope=scope, grants=tuple(grants))])
    shown = set(visible_tools(policies, CATALOG, "a"))
    callable_ = {
        e for e in CATALOG.tools if evaluate(policies, "a", e.tool, e.semver, e.behavior).allowed
    }
    assert shown == callable_
    assert all(not e.behavior.changes_state for e in shown) or scope is Scope.WRITE
