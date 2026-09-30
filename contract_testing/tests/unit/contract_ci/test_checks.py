from datetime import UTC, datetime

import pytest

from adapter_verify.contract_ci.adapters.files import load_rulebook
from adapter_verify.contract_ci.domain.checks import (
    CheckResult,
    Status,
    canonical_check,
    gate,
    mapping_check,
    orphan_check,
    upstream_check,
)
from adapter_verify.contract_ci.domain.contracts import (
    CanonicalContract,
    ContractField,
    ContractType,
    ReleaseLock,
)
from adapter_verify.contract_ci.domain.report import render_markdown
from tests.unit.contract_ci.test_differ import FIRES

pytestmark = pytest.mark.unit

BOOK = load_rulebook()
_AT = datetime(2026, 9, 1, tzinfo=UTC)


def contract(
    version: str, *extra: ContractField, values: tuple[str, ...] = ("open", "closed")
) -> CanonicalContract:
    return CanonicalContract(
        tool="order.get",
        version=version,
        fields=(
            ContractField(name="id", type=ContractType.STRING),
            ContractField(name="status", type=ContractType.ENUM, values=values),
            *extra,
        ),
    )


def lock(*released: CanonicalContract) -> ReleaseLock:
    return ReleaseLock(released={c.key: c.shape(_AT).content_hash() for c in released})


def statuses(results: list[CheckResult]) -> dict[str, Status]:
    return {r.subject: r.status for r in results}


def test_upstream_check_fails_closed_without_baseline_or_samples() -> None:
    base, rev = FIRES["OUTPUT_FIELD_REMOVED"]
    assert (
        upstream_check("s", ["t"], None, rev, BOOK, rename_threshold=0.6).status is Status.UNKNOWN
    )
    unparseable = upstream_check(
        "s", ["t"], base, None, BOOK, rename_threshold=0.6, extraction_error="bad header"
    )
    assert unparseable.status is Status.UNKNOWN
    assert unparseable.findings[0].rule_id == "SOURCE_UNPARSEABLE"
    assert (
        upstream_check("s", ["t"], base, rev, BOOK, rename_threshold=0.6).status is Status.BLOCKED
    )


def test_released_contracts_are_immutable() -> None:
    v1 = contract("1.2.0")
    edited = contract("1.2.0", values=("open", "closed", "held"))
    assert statuses(canonical_check([v1], lock(v1), BOOK, at=_AT)) == {
        "order.get@1.2.0": Status.PASS
    }
    (result,) = canonical_check([edited], lock(v1), BOOK, at=_AT)
    assert (result.status, result.findings[0].rule_id) == (
        Status.BLOCKED,
        "CONTRACT_RELEASED_VERSION_EDITED",
    )
    (deleted,) = canonical_check([], lock(v1), BOOK, at=_AT)
    assert deleted.findings[0].rule_id == "CONTRACT_RELEASED_VERSION_DELETED"


def test_version_bump_rules() -> None:
    v1 = contract("1.2.0")
    minor_enum = contract("1.3.0", values=("open", "closed", "held"))
    patch_enum = contract("1.2.1", values=("open", "closed", "held"))
    minor_break = contract("1.3.0", values=("open",)).model_copy(
        update={"fields": contract("1.3.0").fields[:1]}
    )
    major_break = minor_break.model_copy(update={"version": "2.0.0"})

    def check(rev: CanonicalContract) -> CheckResult:
        return canonical_check([v1, rev], lock(v1), BOOK, at=_AT)[-1]

    assert check(minor_enum).status is Status.PASS
    assert check(patch_enum).status is Status.REVIEW_REQUIRED
    assert check(minor_break).status is Status.BLOCKED
    major = check(major_break)
    assert major.status is Status.PASS
    assert major.note is not None
    assert "MAJOR" in major.note
    assert (
        canonical_check([v1], ReleaseLock(), BOOK, at=_AT)[0].note == "first version of this tool"
    )


def test_mapping_replay_fails_closed_only_with_mappings() -> None:
    assert mapping_check(mappings_present=False).status is Status.PASS
    assert mapping_check(mappings_present=True).status is Status.UNKNOWN


def test_orphans() -> None:
    clean = orphan_check({"a"}, {"a"}, {"a"}, {"src": ["a"]})
    assert clean.status is Status.PASS
    dirty = orphan_check({"a", "b"}, {"a", "c"}, {"a", "d"}, {"src": ["z"]})
    assert {f.rule_id for f in dirty.findings} == {
        "ORPHAN_TOOL_WITHOUT_CONTRACT",
        "ORPHAN_CONTRACT_WITHOUT_TOOL",
        "ORPHAN_TOOL_WITHOUT_CREDENTIAL",
        "ORPHAN_CREDENTIAL_WITHOUT_TOOL",
        "ORPHAN_SOURCE_WITHOUT_TOOL",
    }


@pytest.mark.parametrize(
    ("rule", "approved", "status", "code"),
    [
        (None, False, Status.PASS, 0),
        ("OUTPUT_FIELD_ADDED", False, Status.PASS, 0),
        ("OUTPUT_BECAME_NULLABLE", False, Status.REVIEW_REQUIRED, 2),
        ("OUTPUT_BECAME_NULLABLE", True, Status.REVIEW_REQUIRED, 0),
        ("OUTPUT_FIELD_REMOVED", True, Status.BLOCKED, 1),
    ],
)
def test_gate_exit_codes(rule: str | None, approved: bool, status: Status, code: int) -> None:
    base, rev = FIRES[rule] if rule else FIRES["OUTPUT_FIELD_ADDED"][:1] * 2
    result = gate(
        [upstream_check("s", ["order.get"], base, rev, BOOK, rename_threshold=0.6)],
        review_approved=approved,
    )
    assert (result.status, result.exit_code) == (status, code)
    assert result.review_override == (approved and status is Status.REVIEW_REQUIRED)


def test_unknown_outranks_everything() -> None:
    results = [
        upstream_check("a", [], *FIRES["OUTPUT_FIELD_REMOVED"], BOOK, rename_threshold=0.6),
        mapping_check(mappings_present=True),
    ]
    assert gate(results, review_approved=True).exit_code == 3


def test_report_names_rules_paths_and_agents_to_retest() -> None:
    base, rev = FIRES["OUTPUT_FIELD_REMOVED"]
    result = gate(
        [upstream_check("orders.rest", ["order.get"], base, rev, BOOK, rename_threshold=0.6)],
        review_approved=False,
    )
    markdown = render_markdown(result, {"order.get": ["order-status-agent"], "other": ["x"]})
    assert "BLOCKED (exit 1)" in markdown
    assert "`OUTPUT_FIELD_REMOVED`" in markdown
    assert "`qty`" in markdown
    assert "Agents to retest: `order-status-agent`." in markdown
    assert "`x`" not in markdown


def test_report_details_the_breaking_changes_of_a_major_bump() -> None:
    v1 = contract("1.2.0")
    v2 = contract("2.0.0").model_copy(update={"fields": contract("2.0.0").fields[:1]})
    result = gate(canonical_check([v1, v2], lock(v1), BOOK, at=_AT), review_approved=False)
    assert result.exit_code == 0
    markdown = render_markdown(result, {"order.get": ["order-status-agent"]})
    assert "BREAKING" in markdown
    assert "Agents to retest: `order-status-agent`." in markdown
