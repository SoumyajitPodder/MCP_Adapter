"""Ports the access service depends on."""

from typing import Protocol

from pydantic import AwareDatetime, Field, SecretStr

from adapter_verify.access.domain.policy import AGENT_ID_PATTERN
from adapter_verify.common.model import FrozenModel


class TokenRejectedError(Exception):
    """The token could not be verified. Deliberately carries no detail (fail closed, §9.1)."""


class SecretUnavailableError(Exception):
    """A scoped credential could not be obtained. Carries no detail."""


class VerifiedIdentity(FrozenModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    issuer: str
    expires_at: AwareDatetime


class TokenVerifier(Protocol):
    async def verify(self, token: SecretStr) -> VerifiedIdentity:
        """Check signature, issuer, audience, expiry and not-before. Raises TokenRejectedError."""
        ...


class SecretManager(Protocol):
    async def get(self, name: str) -> SecretStr:
        """Fetch one secret by name. Raises SecretUnavailableError."""
        ...
