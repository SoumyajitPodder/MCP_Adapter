"""Access policies and the tool catalog they are checked against (brief §9.2)."""

import hashlib
from collections.abc import Iterable
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from adapter_kernel.shape import canonical_json
from adapter_kernel.tooldef import Behavior
from adapter_verify.access.domain.semver import Version, VersionRange
from adapter_verify.common.model import FrozenModel

AGENT_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"


class Grant(FrozenModel):
    """Permission to call one tool within one version range. Tool names match exactly."""

    tool: str = Field(min_length=1, description="Exact tool name; wildcards are a lint error.")
    versions: str = Field(description='SemVer range, e.g. ">=1.0.0,<2.0.0".')

    @field_validator("versions")
    @classmethod
    def _parses(cls, value: str) -> str:
        VersionRange.parse(value)
        return value

    @property
    def range(self) -> VersionRange:
        return VersionRange.parse(self.versions)


class Policy(FrozenModel):
    """Everything one agent may do. Deny by default: anything not granted is refused."""

    agent_id: str = Field(pattern=AGENT_ID_PATTERN, description="Authenticated agent identity.")
    owner: str = Field(description="Team accountable for this agent. Required for write scope.")
    scope: Scope = Field(description="read agents may never call state-changing tools.")
    grants: tuple[Grant, ...] = Field(description="Tool and version grants.")


class PolicySet:
    """An immutable, indexed set of policies. Swapped whole on reload, never mutated."""

    def __init__(self, policies: Iterable[Policy]) -> None:
        by_agent: dict[str, Policy] = {}
        for policy in policies:
            if policy.agent_id in by_agent:
                msg = f"duplicate policy for agent {policy.agent_id}"
                raise ValueError(msg)
            by_agent[policy.agent_id] = policy
        self._by_agent = by_agent
        ordered = [by_agent[k].model_dump(mode="json") for k in sorted(by_agent)]
        self.digest = hashlib.sha256(canonical_json(ordered)).hexdigest()

    def get(self, agent_id: str) -> Policy | None:
        return self._by_agent.get(agent_id)

    def __len__(self) -> int:
        return len(self._by_agent)

    def callers(self) -> dict[str, list[str]]:
        """Tool name → agents granted any version of it (the §5.8 "who retests" join)."""
        found: dict[str, set[str]] = {}
        for agent_id, policy in self._by_agent.items():
            for grant in policy.grants:
                found.setdefault(grant.tool, set()).add(agent_id)
        return {tool: sorted(agents) for tool, agents in found.items()}


class CatalogEntry(FrozenModel):
    """One released tool version. Stand-in for the tool registry (brief §1) until it exists."""

    tool: str = Field(min_length=1)
    version: str = Field(description="SemVer version.")
    behavior: Behavior
    default: bool = Field(default=False, description="Served when the caller names no version.")

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        Version.parse(value)
        return value

    @property
    def semver(self) -> Version:
        return Version.parse(self.version)


class ToolCatalog(FrozenModel):
    tools: tuple[CatalogEntry, ...]

    @model_validator(mode="after")
    def _one_default_per_tool(self) -> Self:
        seen: set[tuple[str, str]] = set()
        defaults: dict[str, int] = {}
        for entry in self.tools:
            key = (entry.tool, entry.version)
            if key in seen:
                msg = f"duplicate catalog entry {entry.tool}@{entry.version}"
                raise ValueError(msg)
            seen.add(key)
            defaults[entry.tool] = defaults.get(entry.tool, 0) + entry.default
        missing = sorted(t for t, n in defaults.items() if n != 1)
        if missing:
            msg = f"each tool needs exactly one default version: {', '.join(missing)}"
            raise ValueError(msg)
        return self

    def resolve(self, tool: str, requested: str | None) -> CatalogEntry | None:
        for entry in self.tools:
            if entry.tool == tool and (
                entry.default if requested is None else entry.version == requested
            ):
                return entry
        return None

    def names(self) -> set[str]:
        return {e.tool for e in self.tools}

    def entries(self, tool: str) -> list[CatalogEntry]:
        return [e for e in self.tools if e.tool == tool]
