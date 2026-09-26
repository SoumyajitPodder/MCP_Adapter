"""In-memory access ports for tests and the golden sandbox."""

from datetime import UTC, datetime

from pydantic import SecretStr

from adapter_verify.access.ports import SecretUnavailableError, TokenRejectedError, VerifiedIdentity

SENTINEL_SECRET_PREFIX = "SENTINEL-secret-"  # noqa: S105 - test sentinel, not a credential


class StaticTokenVerifier:
    """Accepts exactly the tokens it was given, mapped to agent IDs."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    async def verify(self, token: SecretStr) -> VerifiedIdentity:
        agent = self._tokens.get(token.get_secret_value())
        if agent is None:
            raise TokenRejectedError
        return VerifiedIdentity(
            agent_id=agent, issuer="synthetic", expires_at=datetime(2100, 1, 1, tzinfo=UTC)
        )


class SentinelSecretManager:
    """Returns sentinel values, so any secret that reaches a sink is caught by the scan (§6.2)."""

    def __init__(self, known: set[str] | None = None) -> None:
        self._known = known
        self.requests: list[str] = []

    async def get(self, name: str) -> SecretStr:
        self.requests.append(name)
        if self._known is not None and name not in self._known:
            raise SecretUnavailableError
        return SecretStr(f"{SENTINEL_SECRET_PREFIX}{name}")
