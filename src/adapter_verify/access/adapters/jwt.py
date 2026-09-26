"""JWT verification against a JWKS endpoint (brief §9.1, D-037).

Fail closed everywhere: unknown algorithm, unknown key, unreachable or stale key set, or any
claim problem rejects the token without saying why.
"""

import asyncio
import json
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

import jwt
from pydantic import SecretStr, ValidationError

from adapter_verify.access.ports import TokenRejectedError, VerifiedIdentity
from adapter_verify.common.ports import Clock

# Asymmetric only: "none" and HS* are never accepted (no algorithm confusion).
ALLOWED_ALGORITHMS: Final = ("RS256", "ES256", "EdDSA")
REQUIRED_CLAIMS: Final = ("exp", "iat", "iss", "aud", "sub")

type JwksFetcher = Callable[[], Awaitable[dict[str, object]]]


def https_jwks_fetcher(url: str, timeout_s: float) -> JwksFetcher:
    if not url.startswith("https://"):
        msg = "JWKS URL must use https"
        raise ValueError(msg)

    def fetch_blocking() -> dict[str, object]:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - https enforced above
            body: dict[str, object] = json.loads(response.read())
            return body

    async def fetch() -> dict[str, object]:
        return await asyncio.to_thread(fetch_blocking)

    return fetch


class JwksCache:
    """Signing keys by ``kid``, refreshed after ``ttl_s``. A stale set is never used."""

    def __init__(
        self, fetch: JwksFetcher, clock: Clock, *, ttl_s: float, min_refresh_s: float
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl_s = ttl_s
        self._min_refresh_s = min_refresh_s
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = self._clock.monotonic()
            fresh = self._fetched_at is not None and now - self._fetched_at < self._ttl_s
            # Refresh on expiry, or on an unknown kid (key rotation) at most every min_refresh_s,
            # so a flood of random kids cannot hammer the identity provider.
            recent = self._fetched_at is not None and now - self._fetched_at < self._min_refresh_s
            if not fresh or (kid not in self._keys and not recent):
                await self._refresh(now)
            if self._fetched_at is None or now - self._fetched_at >= self._ttl_s:
                raise TokenRejectedError
            found = self._keys.get(kid)
            if found is None:
                raise TokenRejectedError
            return found

    async def _refresh(self, now: float) -> None:
        try:
            key_set = jwt.PyJWKSet.from_dict(await self._fetch())
        except Exception:  # noqa: BLE001 - any fetch or parse failure: keep old set, which ages out
            return
        self._keys = {k.key_id: k for k in key_set.keys if k.key_id}
        self._fetched_at = now


class JwtTokenVerifier:
    def __init__(self, keys: JwksCache, *, issuer: str, audience: str, leeway_s: int) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience
        self._leeway_s = leeway_s

    async def verify(self, token: SecretStr) -> VerifiedIdentity:
        raw = token.get_secret_value()
        try:
            header = jwt.get_unverified_header(raw)
            algorithm, kid = header.get("alg"), header.get("kid")
            if algorithm not in ALLOWED_ALGORITHMS or not isinstance(kid, str):
                raise TokenRejectedError
            key = await self._keys.key(kid)
            claims = jwt.decode(
                raw,
                key.key,
                algorithms=[algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_s,
                options={"require": list(REQUIRED_CLAIMS)},
            )
            return VerifiedIdentity(
                agent_id=claims["sub"],
                issuer=claims["iss"],
                expires_at=datetime.fromtimestamp(claims["exp"], UTC),
            )
        except (jwt.PyJWTError, ValidationError, KeyError, TypeError, ValueError):
            raise TokenRejectedError from None
