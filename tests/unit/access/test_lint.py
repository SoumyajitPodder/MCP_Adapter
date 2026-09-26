import pytest

from adapter_verify.access.domain.lint import RULES, PolicyDocument, lint
from adapter_verify.access.domain.policy import Scope
from tests.unit.access.support import CATALOG, READER, WRITER, document, policy

pytestmark = pytest.mark.unit

_FIRING: dict[str, list[PolicyDocument]] = {
    "POLICY_INVALID_FILE": [PolicyDocument(file_name="x.yaml", policy=None, load_error="bad")],
    "POLICY_FILE_NAME_MISMATCH": [document(READER, "other.yaml")],
    "POLICY_DUPLICATE_AGENT": [document(READER), document(READER, "reader-copy.yaml")],
    "POLICY_WILDCARD": [document(policy(grants=(("order.*", ">=1.0.0,<2.0.0"),)))],
    "POLICY_UNKNOWN_TOOL": [document(policy(grants=(("billing.get", ">=1.0.0,<2.0.0"),)))],
    "POLICY_UNKNOWN_VERSION": [document(policy(grants=(("order.get", ">=5.0.0,<6.0.0"),)))],
    "POLICY_CROSS_MAJOR": [document(policy(grants=(("order.get", ">=1.0.0"),)))],
    "POLICY_WRITE_WITHOUT_OWNER": [document(policy(scope=Scope.WRITE, owner=" "))],
    "POLICY_READ_SCOPE_MUTATING_GRANT": [
        document(policy(grants=(("order.cancel", ">=1.0.0,<2.0.0"),)))
    ],
}


def test_every_rule_has_a_firing_case() -> None:
    assert set(_FIRING) == set(RULES)


@pytest.mark.parametrize("rule", sorted(_FIRING))
def test_rule_fires(rule: str) -> None:
    assert rule in {f.rule_id for f in lint(_FIRING[rule], CATALOG)}


def test_clean_policies_have_no_findings() -> None:
    assert lint([document(READER), document(WRITER)], CATALOG) == []


def test_findings_carry_file_and_grant() -> None:
    (finding,) = lint(_FIRING["POLICY_UNKNOWN_TOOL"], CATALOG)
    assert finding.file_name == "reader.yaml"
    assert "billing.get" in finding.detail
