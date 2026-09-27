from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    Next,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_kernel.tooldef import Behavior
from adapter_verify import composition
from adapter_verify.access.fakes import SentinelSecretManager, StaticTokenVerifier
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.idempotency.fakes import (
    MemoryIdempotencyStore,
    MemoryOwnerAlerts,
)
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.fakes import (
    MemoryAuditStore,
    MemoryDiagnostics,
    MemoryEventSink,
    MemoryPayloadStore,
    RecordingTelemetry,
)
from adapter_verify.pipeline import chain
from adapter_verify.settings import AccessSettings, IdempotencySettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

REPO = Path(__file__).resolve().parents[2]


def _ctx() -> CallContext:
    request = RequestContext(
        correlation_id="c",
        correlation_id_generated=False,
        trace_id="t",
        tool="order.get",
        requested_version=None,
        idempotency_key=None,
    )
    return CallContext(
        request=request, agent_id="a", semantic_version="1.2.0", behavior=Behavior.READ_ONLY
    )


def _ok(ctx: CallContext) -> ToolResult:
    return ToolResult(
        outcome=ToolSuccess(content={}, meta=ResponseMeta(correlation_id=ctx.correlation_id)),
        delivery=DeliveryStatus.ACKED,
    )


class Recorder:
    def __init__(self, name: str, log: list[str], *, stop: bool = False) -> None:
        self.name = name
        self._log = log
        self._stop = stop

    async def __call__(self, ctx: CallContext, request: ToolRequest, call_next: Next) -> ToolResult:
        self._log.append(self.name)
        return _ok(ctx) if self._stop else await call_next(ctx, request)


async def test_chain_runs_stages_in_order_and_allows_short_circuit() -> None:
    log: list[str] = []

    async def terminal(ctx: CallContext, request: ToolRequest) -> ToolResult:
        del request
        log.append("terminal")
        return _ok(ctx)

    await chain([Recorder("a", log), Recorder("b", log)], terminal)(
        _ctx(), ToolRequest(arguments={})
    )
    assert log == ["a", "b", "terminal"]
    log.clear()
    await chain([Recorder("a", log, stop=True), Recorder("b", log)], terminal)(
        _ctx(), ToolRequest(arguments={})
    )
    assert log == ["a"]


async def test_composition_wires_access_then_idempotency_from_repository_config() -> None:
    settings = AccessSettings(
        policies_dir=REPO / "policies",
        catalog_path=REPO / "catalog" / "tools.yaml",
        credentials_path=REPO / "credentials.yaml",
    )
    clock = ManualClock(datetime(2026, 9, 25, tzinfo=UTC))
    access = composition.build_access(
        settings,
        verifier=StaticTokenVerifier({"t": "order-status-agent"}),
        secrets=SentinelSecretManager(),
        audit=AuditTrail(MemoryAuditStore(), clock),
        telemetry=RecordingTelemetry(),
        events=MemoryEventSink(),
        diagnostics=MemoryDiagnostics(),
        clock=clock,
    )
    idempotency = composition.build_idempotency(
        IdempotencySettings(),
        store=MemoryIdempotencyStore(),
        payloads=MemoryPayloadStore(),
        audit=AuditTrail(MemoryAuditStore(), clock),
        telemetry=RecordingTelemetry(),
        events=MemoryEventSink(),
        diagnostics=MemoryDiagnostics(),
        alerts=MemoryOwnerAlerts(),
        clock=clock,
        entropy=SeededEntropy(b"pipeline"),
    )
    reached: list[CallContext] = []

    async def terminal(ctx: CallContext, request: ToolRequest) -> ToolResult:
        del request
        reached.append(ctx)
        return _ok(ctx)

    pipeline = composition.build_pipeline(access, idempotency, terminal)
    assert pipeline.stage_names == ("access.check", "idempotency.reserve")

    request = _ctx().request
    ok = await pipeline(request, ToolRequest(arguments={}), SecretStr("t"))
    assert isinstance(ok.outcome, ToolSuccess)
    assert reached[0].agent_id == "order-status-agent"

    denied = await pipeline(
        request.model_copy(update={"tool": "inventory.snapshot"}),
        ToolRequest(arguments={}),
        SecretStr("t"),
    )
    assert isinstance(denied.outcome, ToolFailure)
    assert denied.outcome.error.code is ErrorCode.NOT_AUTHORIZED
    assert len(reached) == 1


async def test_jwt_verifier_requires_complete_settings() -> None:
    clock = ManualClock(datetime(2026, 9, 25, tzinfo=UTC))
    with pytest.raises(ValueError, match="all required"):
        composition.token_verifier(AccessSettings(), clock)
    verifier = composition.token_verifier(
        AccessSettings(jwt_issuer="i", jwt_audience="a", jwks_url="https://idp.invalid/jwks"), clock
    )
    assert verifier is not None
