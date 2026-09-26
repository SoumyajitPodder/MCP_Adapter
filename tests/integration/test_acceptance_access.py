"""Brief §9.10 acceptance on real Postgres: an ungranted agent is blocked at both enforcement
points, cannot see the tool, the denial is audited with its rule ID, and no secret reaches any
sink. Tokens are real JWTs signed with keys generated for this run."""

import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr

from adapter_kernel.context import CallContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolFailure, ToolRequest, ToolResult, ToolSuccess
from adapter_verify import composition
from adapter_verify.access.adapters.jwt import JwksCache, JwtTokenVerifier
from adapter_verify.access.fakes import SENTINEL_SECRET_PREFIX, SentinelSecretManager
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.idempotency.fakes import MemoryIdempotencyStore, MemoryOwnerAlerts
from adapter_verify.observability.adapters.postgres import PostgresAuditStore
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.fakes import (
    MemoryDiagnostics,
    MemoryEventSink,
    MemoryPayloadStore,
    RecordingTelemetry,
)
from adapter_verify.observability.service import META_CORRELATION_ID, InboundCall, ObservedEntry
from adapter_verify.settings import AccessSettings, IdempotencySettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REPO = Path(__file__).resolve().parents[2]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER, AUDIENCE = "https://idp.synthetic.invalid", "mcp-adapter"


async def _jwks() -> dict[str, object]:
    return {
        "keys": [
            {**RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1", "alg": "RS256"}
        ]
    }


def _token(agent: str) -> SecretStr:
    now = int(time.time())
    claims = {"sub": agent, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300}
    return SecretStr(jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"}))


async def test_ungranted_agent_is_blocked_hidden_audited_and_nothing_leaks(
    pool: asyncpg.Pool,
) -> None:
    clock = ManualClock(datetime(2026, 9, 25, tzinfo=UTC))
    telemetry, events, diagnostics = RecordingTelemetry(), MemoryEventSink(), MemoryDiagnostics()
    secrets = SentinelSecretManager()
    verifier = JwtTokenVerifier(
        JwksCache(_jwks, clock, ttl_s=300, min_refresh_s=30),
        issuer=ISSUER,
        audience=AUDIENCE,
        leeway_s=60,
    )
    access = composition.build_access(
        AccessSettings(
            policies_dir=REPO / "policies",
            catalog_path=REPO / "catalog" / "tools.yaml",
            credentials_path=REPO / "credentials.yaml",
        ),
        verifier=verifier,
        secrets=secrets,
        audit=AuditTrail(PostgresAuditStore(pool), clock),
        telemetry=telemetry,
        events=events,
        diagnostics=diagnostics,
        clock=clock,
    )
    backend_calls: list[str] = []

    async def connector(ctx: CallContext, request: ToolRequest) -> ToolResult:
        del request
        credential = await access.credentials.credential_for(ctx)  # scoped per tool (§9.7)
        assert credential.get_secret_value().startswith(SENTINEL_SECRET_PREFIX)
        backend_calls.append(ctx.tool)
        return ToolResult(
            outcome=ToolSuccess(
                content={"status": "ok"}, meta=ResponseMeta(correlation_id=ctx.correlation_id)
            )
        )

    entry = ObservedEntry(
        telemetry=telemetry,
        events=events,
        diagnostics=diagnostics,
        clock=clock,
        entropy=SeededEntropy(b"acceptance"),
        downstream=composition.build_pipeline(
            access,
            composition.build_idempotency(
                IdempotencySettings(),
                store=MemoryIdempotencyStore(),
                payloads=MemoryPayloadStore(),
                audit=AuditTrail(PostgresAuditStore(pool), clock),
                telemetry=telemetry,
                events=events,
                diagnostics=diagnostics,
                alerts=MemoryOwnerAlerts(),
                clock=clock,
                entropy=SeededEntropy(b"idempotency"),
            ),
            connector,
        ),
    )

    async def call(tool: str, cid: str) -> ToolResult:
        return await entry.handle(
            InboundCall(
                tool=tool,
                arguments={},
                meta={META_CORRELATION_ID: cid},
                credential=_token("order-status-agent"),
            )
        )

    # Granted tool: served, using the tool's own scoped credential.
    granted = await call("order.get", "job-1")
    assert isinstance(granted.outcome, ToolSuccess)
    assert secrets.requests == ["order-management/read"]

    # Ungranted tool: blocked at tools/call, never reaches the backend.
    blocked = await call("inventory.snapshot", "job-2")
    assert isinstance(blocked.outcome, ToolFailure)
    assert blocked.outcome.error.code is ErrorCode.NOT_AUTHORIZED
    assert backend_calls == ["order.get"]

    # ...and hidden at tools/list.
    listed = await access.authenticator.list_tools(_token("order-status-agent"))
    assert listed is not None
    assert "inventory.snapshot" not in {e.tool for e in listed}

    # The denial is audited with its rule ID and correlation ID.
    rows = await pool.fetch(
        "SELECT event::text AS event FROM audit_log WHERE chain_id = 'agent:order-status-agent'"
    )
    assert len(rows) == 1
    assert "ACCESS_TOOL_NOT_GRANTED" in rows[0]["event"]
    assert "job-2" in rows[0]["event"]

    # No secret sentinel in the durable audit store; in-memory sinks are scanned by the plugin.
    everything = await pool.fetch("SELECT event::text AS event FROM audit_log")
    assert all("SENTINEL" not in r["event"] for r in everything)
    assert all("SENTINEL" not in str(e) for e in events.events)
