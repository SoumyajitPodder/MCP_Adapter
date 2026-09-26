"""Policy lint (brief §9.3). Any finding fails CI and stops a policy set from loading."""

import re
from collections import Counter
from collections.abc import Sequence
from typing import Final

from adapter_verify.access.domain.policy import Policy, Scope, ToolCatalog
from adapter_verify.common.model import FrozenModel

_WILDCARD = re.compile(r"[*?\[\]{}()|+^$\\]")

RULES: Final[dict[str, str]] = {
    "POLICY_INVALID_FILE": "file is not a valid policy",
    "POLICY_FILE_NAME_MISMATCH": "file name must equal <agent_id>.yaml",
    "POLICY_DUPLICATE_AGENT": "two files declare the same agent_id",
    "POLICY_WILDCARD": "tool names must be exact; wildcards are forbidden",
    "POLICY_UNKNOWN_TOOL": "grant names a tool that is not in the catalog",
    "POLICY_UNKNOWN_VERSION": "grant range matches no released version",
    "POLICY_CROSS_MAJOR": "range must be bounded inside one MAJOR (>=N.x.y,<N+1.0.0)",
    "POLICY_WRITE_WITHOUT_OWNER": "write scope requires an owner",
    "POLICY_READ_SCOPE_MUTATING_GRANT": "read scope granted a state-changing tool",
}


class PolicyDocument(FrozenModel):
    """A policy file as loaded: the parsed policy, or why it could not be parsed."""

    file_name: str
    policy: Policy | None
    load_error: str | None


class LintFinding(FrozenModel):
    rule_id: str
    file_name: str
    detail: str


def lint(documents: Sequence[PolicyDocument], catalog: ToolCatalog) -> list[LintFinding]:
    findings: list[LintFinding] = []

    def add(rule: str, file_name: str, detail: str = "") -> None:
        findings.append(
            LintFinding(rule_id=rule, file_name=file_name, detail=detail or RULES[rule])
        )

    counts = Counter(d.policy.agent_id for d in documents if d.policy is not None)
    for doc in sorted(documents, key=lambda d: d.file_name):
        if doc.policy is None:
            add(
                "POLICY_INVALID_FILE", doc.file_name, doc.load_error or RULES["POLICY_INVALID_FILE"]
            )
            continue
        policy = doc.policy
        if doc.file_name.rsplit(".", 1)[0] != policy.agent_id:
            add("POLICY_FILE_NAME_MISMATCH", doc.file_name)
        if counts[policy.agent_id] > 1:
            add("POLICY_DUPLICATE_AGENT", doc.file_name, f"agent {policy.agent_id}")
        if policy.scope is Scope.WRITE and not policy.owner.strip():
            add("POLICY_WRITE_WITHOUT_OWNER", doc.file_name)
        for grant in policy.grants:
            where = f"{grant.tool} {grant.versions}"
            if _WILDCARD.search(grant.tool):
                add("POLICY_WILDCARD", doc.file_name, where)
                continue
            if grant.range.single_major() is None:
                add("POLICY_CROSS_MAJOR", doc.file_name, where)
            entries = catalog.entries(grant.tool)
            if not entries:
                add("POLICY_UNKNOWN_TOOL", doc.file_name, where)
                continue
            covered = [e for e in entries if grant.range.contains(e.semver)]
            if not covered:
                add("POLICY_UNKNOWN_VERSION", doc.file_name, where)
            if policy.scope is Scope.READ and any(e.behavior.changes_state for e in covered):
                add("POLICY_READ_SCOPE_MUTATING_GRANT", doc.file_name, where)
    return findings
