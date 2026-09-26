"""JWT verification. Keys are generated per test run; nothing secret is stored in the repo."""

import time
from collections.abc import Callable
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from pydantic import SecretStr

from adapter_verify.access.adapters.jwt import JwksCache, JwtTokenVerifier, https_jwks_fetcher
from adapter_verify.access.ports import TokenRejectedError
from adapter_verify.common.fakes import ManualClock

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ISSUER, AUDIENCE = "https://idp.synthetic.invalid", "mcp-adapter"
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _jwks() -> dict[str, object]:
    rsa_jwk = RSAAlgorithm.to_jwk(RSA_KEY.public_key(), as_dict=True)
    ec_jwk = ECAlgorithm.to_jwk(EC_KEY.public_key(), as_dict=True)
    return {
        "keys": [
            {**rsa_jwk, "kid": "rsa-1", "alg": "RS256"},
            {**ec_jwk, "kid": "ec-1", "alg": "ES256"},
        ]
    }


class Fetcher:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def __call__(self) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise OSError("idp down")
        return _jwks()


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": "order-status-agent",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(
    claims: dict[str, object], *, alg: str = "RS256", kid: str | None = "rsa-1"
) -> SecretStr:
    key = RSA_KEY if alg == "RS256" else EC_KEY
    headers = {} if kid is None else {"kid": kid}
    return SecretStr(jwt.encode(claims, key, algorithm=alg, headers=headers))


def _verifier(fetcher: Fetcher, clock: ManualClock | None = None) -> JwtTokenVerifier:
    cache = JwksCache(
        fetcher, clock or ManualClock(datetime(2026, 1, 1, tzinfo=UTC)), ttl_s=300, min_refresh_s=30
    )
    return JwtTokenVerifier(cache, issuer=ISSUER, audience=AUDIENCE, leeway_s=60)


@pytest.mark.parametrize("alg", ["RS256", "ES256"])
async def test_valid_tokens_verify(alg: str) -> None:
    identity = await _verifier(Fetcher()).verify(
        _token(_claims(), alg=alg, kid="rsa-1" if alg == "RS256" else "ec-1")
    )
    assert identity.agent_id == "order-status-agent"
    assert identity.issuer == ISSUER


_REJECTED: dict[str, Callable[[], SecretStr]] = {
    "expired": lambda: _token(_claims(exp=int(time.time()) - 120)),
    "not yet valid": lambda: _token(_claims(nbf=int(time.time()) + 600)),
    "wrong audience": lambda: _token(_claims(aud="someone-else")),
    "wrong issuer": lambda: _token(_claims(iss="https://evil.invalid")),
    "missing sub": lambda: _token(_claims(sub=None)),
    "missing exp": lambda: _token(_claims(exp=None)),
    "bad agent id": lambda: _token(_claims(sub="has space")),
    "unknown kid": lambda: _token(_claims(), kid="rotated-away"),
    "no kid": lambda: _token(_claims(), kid=None),
    "key/alg mismatch": lambda: _token(_claims(), alg="ES256", kid="rsa-1"),
    "alg none": lambda: SecretStr(
        jwt.encode(_claims(), "", algorithm="none", headers={"kid": "rsa-1"})
    ),
    "HS256 confusion": lambda: SecretStr(
        jwt.encode(_claims(), "shared-" + "x" * 32, algorithm="HS256", headers={"kid": "rsa-1"})
    ),
    "garbage": lambda: SecretStr("not.a.jwt"),
}


@pytest.mark.parametrize("case", sorted(_REJECTED))
async def test_bad_tokens_are_rejected_without_detail(case: str) -> None:
    with pytest.raises(TokenRejectedError) as caught:
        await _verifier(Fetcher()).verify(_REJECTED[case]())
    assert str(caught.value) == ""
    assert caught.value.__cause__ is None


async def test_skew_is_tolerated_within_leeway() -> None:
    token = _token(_claims(exp=int(time.time()) - 30))
    assert (await _verifier(Fetcher()).verify(token)).agent_id == "order-status-agent"


async def test_keys_are_cached_and_stale_keys_are_never_used() -> None:
    fetcher, clock = Fetcher(), ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    verifier = _verifier(fetcher, clock)
    await verifier.verify(_token(_claims()))
    await verifier.verify(_token(_claims()))
    assert fetcher.calls == 1

    fetcher.fail = True
    clock.advance(301)
    with pytest.raises(TokenRejectedError):
        await verifier.verify(_token(_claims()))

    fetcher.fail = False
    assert (await verifier.verify(_token(_claims()))).agent_id == "order-status-agent"


async def test_unknown_kids_cannot_force_constant_refetching() -> None:
    fetcher = Fetcher()
    verifier = _verifier(fetcher)
    for _ in range(5):
        with pytest.raises(TokenRejectedError):
            await verifier.verify(_token(_claims(), kid="random"))
    assert fetcher.calls == 1


async def test_jwks_url_must_be_https() -> None:
    with pytest.raises(ValueError, match="https"):
        https_jwks_fetcher("http://idp.invalid/jwks", timeout_s=1)
