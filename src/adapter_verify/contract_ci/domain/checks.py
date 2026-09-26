"""The four checks (brief §5.6) and the merge gate (§5.7). Pure."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import Field

from adapter_kernel.classification import Classification, worst
from adapter_kernel.shape import ProvenanceKind, Shape
from adapter_verify.common.model import FrozenModel
from adapter_verify.contract_ci.domain.contracts import CanonicalContract, ReleaseLock
from adapter_verify.contract_ci.domain.differ import Aliases, Finding, classify, diff
from adapter_verify.contract_ci.domain.rules import CheckKind, RuleBook

RULE_BASELINE_MISSING: Final = "BASELINE_MISSING"
RULE_SOURCE_UNPARSEABLE: Final = "SOURCE_UNPARSEABLE"
RULE_RELEASED_EDITED: Final = "CONTRACT_RELEASED_VERSION_EDITED"
RULE_RELEASED_DELETED: Final = "CONTRACT_RELEASED_VERSION_DELETED"
RULE_MAPPING_REPLAY_UNAVAILABLE: Final = "MAPPING_REPLAY_UNAVAILABLE"
RULE_ORPHAN_TOOL_WITHOUT_CONTRACT: Final = "ORPHAN_TOOL_WITHOUT_CONTRACT"
RULE_ORPHAN_CONTRACT_WITHOUT_TOOL: Final = "ORPHAN_CONTRACT_WITHOUT_TOOL"
RULE_ORPHAN_TOOL_WITHOUT_CREDENTIAL: Final = "ORPHAN_TOOL_WITHOUT_CREDENTIAL"
RULE_ORPHAN_CREDENTIAL_WITHOUT_TOOL: Final = "ORPHAN_CREDENTIAL_WITHOUT_TOOL"
RULE_ORPHAN_SOURCE_WITHOUT_TOOL: Final = "ORPHAN_SOURCE_WITHOUT_TOOL"


class Check(StrEnum):
    UPSTREAM = "upstream source diff"
    CANONICAL = "canonical contract diff"
    MAPPING = "mapping replay"
    ORPHANS = "orphan check"


class Status(StrEnum):
    PASS = "pass"  # noqa: S105 - a status, not a credential
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


_STATUS = {
    Classification.COMPATIBLE: Status.PASS,
    Classification.REVIEW_REQUIRED: Status.REVIEW_REQUIRED,
    Classification.BREAKING: Status.BLOCKED,
    Classification.UNKNOWN: Status.UNKNOWN,
}
_SEVERITY = [Status.PASS, Status.REVIEW_REQUIRED, Status.BLOCKED, Status.UNKNOWN]
EXIT_CODES: Final = {
    Status.PASS: 0,
    Status.BLOCKED: 1,
    Status.REVIEW_REQUIRED: 2,
    Status.UNKNOWN: 3,
}


class CheckResult(FrozenModel):
    check: Check
    subject: str = Field(description="source_id, tool@version, or the checked set.")
    tools: tuple[str, ...] = Field(description="Tools whose callers are affected.")
    findings: tuple[Finding, ...]
    status: Status
    note: str | None = None


def _finding(rule_id: str, classification: Classification, detail: str, path: str = "") -> Finding:
    return Finding(
        rule_id=rule_id, operation="", path=path, detail=detail, classification=classification
    )


def _result(
    check: Check,
    subject: str,
    tools: Sequence[str],
    findings: Sequence[Finding],
    note: str | None = None,
) -> CheckResult:
    status = _STATUS[worst(f.classification for f in findings)]
    return CheckResult(
        check=check,
        subject=subject,
        tools=tuple(tools),
        findings=tuple(findings),
        status=status,
        note=note,
    )


def upstream_check(  # noqa: PLR0913 - one source's full context
    source_id: str,
    tools: Sequence[str],
    baseline: Shape | None,
    current: Shape | None,
    book: RuleBook,
    *,
    aliases: Aliases | None = None,
    rename_threshold: float,
    extraction_error: str | None = None,
) -> CheckResult:
    if baseline is None:
        return _result(
            Check.UPSTREAM,
            source_id,
            tools,
            [
                _finding(
                    RULE_BASELINE_MISSING,
                    Classification.UNKNOWN,
                    "no accepted baseline; run `baseline accept`",
                )
            ],
        )
    if current is None:
        return _result(
            Check.UPSTREAM,
            source_id,
            tools,
            [
                _finding(
                    RULE_SOURCE_UNPARSEABLE,
                    Classification.UNKNOWN,
                    extraction_error or "no samples",
                )
            ],
        )
    inferred = ProvenanceKind.INFERRED in {baseline.provenance.kind, current.provenance.kind}
    changes = diff(baseline, current, aliases=aliases, rename_threshold=rename_threshold)
    return _result(
        Check.UPSTREAM,
        source_id,
        tools,
        classify(changes, book, CheckKind.UPSTREAM, inferred=inferred),
    )


def canonical_check(
    contracts: Sequence[CanonicalContract], lock: ReleaseLock, book: RuleBook, *, at: datetime
) -> list[CheckResult]:
    present = {c.key for c in contracts}
    results: list[CheckResult] = [
        _result(
            Check.CANONICAL,
            key,
            [key.split("@")[0]],
            [_finding(RULE_RELEASED_DELETED, Classification.BREAKING, "released contract deleted")],
        )
        for key in sorted(set(lock.released) - present)
    ]
    for contract in sorted(contracts, key=lambda c: (c.tool, c.semver)):
        shape = contract.shape(at)
        released_hash = lock.released.get(contract.key)
        if released_hash is not None:
            findings = (
                []
                if released_hash == shape.content_hash()
                else [
                    _finding(
                        RULE_RELEASED_EDITED,
                        Classification.BREAKING,
                        "released versions are immutable (§5.6)",
                    )
                ]
            )
            results.append(_result(Check.CANONICAL, contract.key, [contract.tool], findings))
            continue
        previous = max(
            (
                c
                for c in contracts
                if c.tool == contract.tool and c.key in lock.released and c.semver < contract.semver
            ),
            key=lambda c: c.semver,
            default=None,
        )
        if previous is None:
            results.append(
                _result(
                    Check.CANONICAL,
                    contract.key,
                    [contract.tool],
                    [],
                    note="first version of this tool",
                )
            )
            continue
        major = contract.semver.major > previous.semver.major
        minor = not major and contract.semver.minor > previous.semver.minor
        findings = classify(
            diff(previous.shape(at), shape),
            book,
            CheckKind.CANONICAL,
            inferred=False,
            minor_bump=minor,
        )
        if major:
            results.append(
                CheckResult(
                    check=Check.CANONICAL,
                    subject=contract.key,
                    tools=(contract.tool,),
                    findings=tuple(findings),
                    status=Status.PASS,
                    note=f"MAJOR bump from {previous.version}; starts in draft (§6 gates it)",
                )
            )
        else:
            results.append(
                _result(
                    Check.CANONICAL,
                    contract.key,
                    [contract.tool],
                    findings,
                    note=f"compared with {previous.version}",
                )
            )
    return results


def mapping_check(*, mappings_present: bool) -> CheckResult:
    """Replay needs the translation engine (§2). Nothing to replay passes; else fail closed."""
    if not mappings_present:
        return _result(Check.MAPPING, "mappings", [], [], note="no mappings in the repository")
    return _result(
        Check.MAPPING,
        "mappings",
        [],
        [
            _finding(
                RULE_MAPPING_REPLAY_UNAVAILABLE,
                Classification.UNKNOWN,
                "translation engine not available",
            )
        ],
    )


def orphan_check(
    catalog_tools: Iterable[str],
    contract_tools: Iterable[str],
    credential_tools: Iterable[str],
    source_tools: Mapping[str, Sequence[str]],
) -> CheckResult:
    catalog, contracts, credentials = set(catalog_tools), set(contract_tools), set(credential_tools)
    findings = [
        _finding(RULE_ORPHAN_TOOL_WITHOUT_CONTRACT, Classification.BREAKING, t, t)
        for t in sorted(catalog - contracts)
    ]
    findings += [
        _finding(RULE_ORPHAN_CONTRACT_WITHOUT_TOOL, Classification.BREAKING, t, t)
        for t in sorted(contracts - catalog)
    ]
    findings += [
        _finding(RULE_ORPHAN_TOOL_WITHOUT_CREDENTIAL, Classification.BREAKING, t, t)
        for t in sorted(catalog - credentials)
    ]
    findings += [
        _finding(RULE_ORPHAN_CREDENTIAL_WITHOUT_TOOL, Classification.BREAKING, t, t)
        for t in sorted(credentials - catalog)
    ]
    findings += [
        _finding(RULE_ORPHAN_SOURCE_WITHOUT_TOOL, Classification.BREAKING, f"{sid} feeds {t}", sid)
        for sid, tools in sorted(source_tools.items())
        for t in tools
        if t not in catalog
    ]
    return _result(Check.ORPHANS, "tools, contracts, credentials, sources", [], findings)


class GateResult(FrozenModel):
    status: Status
    exit_code: int
    review_override: bool = Field(description="REVIEW_REQUIRED passed by the review label (§5.7).")
    results: tuple[CheckResult, ...]


def gate(results: Sequence[CheckResult], *, review_approved: bool) -> GateResult:
    status = max((r.status for r in results), key=_SEVERITY.index, default=Status.PASS)
    override = status is Status.REVIEW_REQUIRED and review_approved
    return GateResult(
        status=status,
        exit_code=0 if override else EXIT_CODES[status],
        review_override=override,
        results=tuple(results),
    )
