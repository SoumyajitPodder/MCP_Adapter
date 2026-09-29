"""Access decisions (brief §9.4, §9.6). Pure: same inputs, same decision, explainable by rule ID."""

from typing import Final

from pydantic import Field

from adapter_kernel.errors import ErrorCode
from adapter_kernel.tooldef import Behavior
from adapter_verify.access.domain.policy import CatalogEntry, PolicySet, Scope, ToolCatalog
from adapter_verify.access.domain.semver import Version
from adapter_verify.common.model import FrozenModel

RULE_TOKEN_MISSING: Final = "ACCESS_TOKEN_MISSING"  # noqa: S105 - a rule ID, not a credential
RULE_TOKEN_INVALID: Final = "ACCESS_TOKEN_INVALID"  # noqa: S105 - a rule ID, not a credential
RULE_UNKNOWN_TOOL: Final = "ACCESS_UNKNOWN_TOOL"
RULE_NO_POLICY: Final = "ACCESS_NO_POLICY"
RULE_TOOL_NOT_GRANTED: Final = "ACCESS_TOOL_NOT_GRANTED"
RULE_VERSION_OUT_OF_RANGE: Final = "ACCESS_VERSION_OUT_OF_RANGE"
RULE_SCOPE_READ_ONLY: Final = "ACCESS_SCOPE_READ_ONLY"
RULE_GRANTED_PREFIX: Final = "ACCESS_GRANTED"


class Decision(FrozenModel):
    allowed: bool
    rule_id: str = Field(description="Stable rule that decided; ACCESS_GRANTED:<agent>/<tool>/<n>.")
    error_code: ErrorCode | None = Field(description="Set exactly when denied.")
    reason: str = Field(description="Fixed explanation; identifiers only, never payload data.")
    policy_digest: str = Field(description="Digest of the policy set that decided (replayable).")


def deny(rule_id: str, code: ErrorCode, reason: str, digest: str) -> Decision:
    return Decision(
        allowed=False, rule_id=rule_id, error_code=code, reason=reason, policy_digest=digest
    )


def evaluate(
    policies: PolicySet, agent_id: str, tool: str, version: Version, behavior: Behavior
) -> Decision:
    digest = policies.digest
    policy = policies.get(agent_id)
    if policy is None:
        return deny(RULE_NO_POLICY, ErrorCode.NOT_AUTHORIZED, "agent has no policy", digest)
    grants = [(i, g) for i, g in enumerate(policy.grants) if g.tool == tool]
    if not grants:
        return deny(RULE_TOOL_NOT_GRANTED, ErrorCode.NOT_AUTHORIZED, "tool not granted", digest)
    matching = [i for i, g in grants if g.range.contains(version)]
    if not matching:
        return deny(
            RULE_VERSION_OUT_OF_RANGE,
            ErrorCode.VERSION_NOT_PERMITTED,
            "version outside every granted range",
            digest,
        )
    if behavior.changes_state and policy.scope is Scope.READ:
        return deny(
            RULE_SCOPE_READ_ONLY,
            ErrorCode.SCOPE_VIOLATION,
            "read scope cannot call a state-changing tool",
            digest,
        )
    return Decision(
        allowed=True,
        rule_id=f"{RULE_GRANTED_PREFIX}:{agent_id}/{tool}/{matching[0]}",
        error_code=None,
        reason="granted",
        policy_digest=digest,
    )


def visible_tools(policies: PolicySet, catalog: ToolCatalog, agent_id: str) -> list[CatalogEntry]:
    """What ``tools/list`` may show: exactly the tool versions ``tools/call`` would allow (§9.5)."""
    return [
        entry
        for entry in catalog.tools
        if evaluate(policies, agent_id, entry.tool, entry.semver, entry.behavior).allowed
    ]
