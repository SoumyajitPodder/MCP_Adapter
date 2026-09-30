"""JWT verification against a JWKS endpoint (brief §9.1, D-037).

Fail closed everywhere: unknown algorithm, unknown key, unreachable or stale key set, or any
claim problem rejects the token without saying why.
"""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from http.client import HTTPMessage
from typing import IO, Final, override

import jwt
from pydantic import SecretStr, ValidationError

from adapter_verify.access.ports import TokenRejectedError, VerifiedIdentity
from adapter_verify.common.ports import Clock

# Asymmetric only: "none" and HS* are never accepted (no algorithm confusion).
ALLOWED_ALGORITHMS: Final = ("RS256", "ES256", "EdDSA")
REQUIRED_CLAIMS: Final = ("exp", "iat", "iss", "aud", "sub")

type JwksFetcher = Callable[[], Awaitable[dict[str, object]]]


MAX_JWKS_BYTES: Final = 1 << 20


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https URL: the https check must hold for the URL the
    keys actually come from, not just the configured one."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "redirect away from https", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_capped(response: IO[bytes], limit: int = MAX_JWKS_BYTES) -> bytes:
    """The body, or ValueError past ``limit`` bytes: a key set is small, a flood is not."""
    body = response.read(limit + 1)
    if len(body) > limit:
        msg = f"JWKS body larger than {limit} bytes"
        raise ValueError(msg)
    return body


def https_jwks_fetcher(url: str, timeout_s: float) -> JwksFetcher:
    if not url.startswith("https://"):
        msg = "JWKS URL must use https"
        raise ValueError(msg)
    opener = urllib.request.build_opener(HttpsOnlyRedirects())

    def fetch_blocking() -> dict[str, object]:
        with opener.open(url, timeout=timeout_s) as response:
            body: dict[str, object] = json.loads(read_capped(response))
            return body

    async def fetch() -> dict[str, object]:
        return await asyncio.to_thread(fetch_blocking)

    return fetch


class JwksCache:
    """Signing keys by ``kid``, refreshed after ``ttl_s``. A stale set is never used.

    A fresh cached key is returned without waiting, even while a refresh is in flight. At most
    one fetch runs at a time; every caller that needs a refresh awaits that same fetch.
    """

    def __init__(
        self, fetch: JwksFetcher, clock: Clock, *, ttl_s: float, min_refresh_s: float
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl_s = ttl_s
        self._min_refresh_s = min_refresh_s
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._inflight: asyncio.Task[None] | None = None

    async def key(self, kid: str) -> jwt.PyJWK:
        if self._fresh() and kid in self._keys:
            return self._keys[kid]
        # Refresh on expiry, or on an unknown kid (key rotation) at most every min_refresh_s,
        # counted from the last attempt, so a flood of random kids cannot hammer the identity
        # provider even while it is failing.
        now = self._clock.monotonic()
        recent = self._attempted_at is not None and now - self._attempted_at < self._min_refresh_s
        if not self._fresh() or self._inflight is not None or not recent:
            await self._refreshed()
        found = self._keys.get(kid)
        if not self._fresh() or found is None:
            raise TokenRejectedError
        return found

    def _fresh(self) -> bool:
        now = self._clock.monotonic()
        return self._fetched_at is not None and now - self._fetched_at < self._ttl_s

    async def _refreshed(self) -> None:
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._refresh())
        # The cache owns the fetch: a cancelled caller stops waiting without cancelling it for
        # everyone else.
        await asyncio.shield(self._inflight)

    async def _refresh(self) -> None:
        started = self._clock.monotonic()
        self._attempted_at = started
        try:
            key_set = jwt.PyJWKSet.from_dict(await self._fetch())
        except Exception:  # noqa: BLE001 - any fetch or parse failure: keep old set, which ages out
            return
        else:
            self._keys = {k.key_id: k for k in key_set.keys if k.key_id}
            self._fetched_at = started
        finally:
            self._inflight = None


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
