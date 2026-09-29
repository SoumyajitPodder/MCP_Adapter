"""Per-tool credential names (brief §9.7, D-037). Names only; values live in the secret manager."""

from typing import Self

from pydantic import Field, field_validator, model_validator

from adapter_verify.access.domain.semver import Version, VersionRange
from adapter_verify.common.model import FrozenModel


class CredentialBinding(FrozenModel):
    tool: str = Field(min_length=1)
    versions: str = Field(description="SemVer range this credential serves.")
    secret_name: str = Field(pattern=r"^[A-Za-z0-9_./-]{1,128}$", description="Name, never value.")

    @field_validator("versions")
    @classmethod
    def _parses(cls, value: str) -> str:
        VersionRange.parse(value)
        return value


class CredentialMap(FrozenModel):
    bindings: tuple[CredentialBinding, ...]

    @model_validator(mode="after")
    def _one_binding_per_tool(self) -> Self:
        tools = [b.tool for b in self.bindings]
        if len(tools) != len(set(tools)):
            msg = "one credential binding per tool; split by version in the tool registry instead"
            raise ValueError(msg)
        return self

    def secret_for(self, tool: str, version: Version) -> str | None:
        for binding in self.bindings:
            if binding.tool == tool and VersionRange.parse(binding.versions).contains(version):
                return binding.secret_name
        return None
