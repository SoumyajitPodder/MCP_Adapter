"""An in-memory world for idempotency tests: the stage, maintenance and every port as a fake.

The pilot catalog is all read-only, so these tests use a synthetic mutating tool.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.pipeline import ToolFailure, ToolRequest, ToolResult
from adapter_kernel.tooldef import Behavior
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.idempotency.domain.keys import fingerprint
from adapter_verify.idempotency.domain.records import RecordKey, Reservation, Timing
from adapter_verify.idempotency.fakes import (
    FaultyConnector,
    MemoryIdempotencyStore,
    MemoryOwnerAlerts,
)
from adapter_verify.idempotency.service import (
    IdempotencyAudit,
    IdempotencyMaintenance,
    IdempotencyObserver,
    IdempotencyStage,
)
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.fakes import (
    MemoryAuditStore,
    MemoryDiagnostics,
    MemoryEventSink,
    MemoryPayloadStore,
    RecordingTelemetry,
)

AT = datetime(2026, 9, 25, tzinfo=UTC)
TIMING = Timing(lease_s=30, retention_h=72)
TOOL = "order.cancel"
ARGS: JsonObject = {"order_id": "o-1", "reason": "customer request"}


def context(
    key: str | None = "k-1",
    *,
    agent: str = "writer",
    tool: str = TOOL,
    version: str = "1.0.0",
    behavior: Behavior = Behavior.MUTATING,
    cid: str = "c-1",
) -> CallContext:
    request = RequestContext(
        correlation_id=cid,
        correlation_id_generated=False,
        trace_id="t",
        tool=tool,
        requested_version=None,
        idempotency_key=key,
    )
    return CallContext(request=request, agent_id=agent, semantic_version=version, behavior=behavior)


def reservation(at: datetime, key: str = "k-1") -> Reservation:
    """A reservation as the stage would make it for ``ARGS``."""
    return Reservation(
        key=RecordKey(agent_id="writer", tool=TOOL, key=key),
        fingerprint=fingerprint(ARGS),
        semantic_version="1.0.0",
        attempt_id=uuid4(),
        correlation_id="c-1",
        at=at,
        lease_expires_at=at + timedelta(seconds=TIMING.lease_s),
        expires_at=at + timedelta(hours=TIMING.retention_h),
    )


def error(result: ToolResult) -> ErrorCode | None:
    return result.outcome.error.code if isinstance(result.outcome, ToolFailure) else None


class World:
    def __init__(self, timing: Timing = TIMING) -> None:
        self.clock = ManualClock(AT)
        self.telemetry = RecordingTelemetry()
        self.events = MemoryEventSink()
        self.audit_store = MemoryAuditStore()
        self.diagnostics = MemoryDiagnostics()
        self.alerts = MemoryOwnerAlerts()
        self.store = MemoryIdempotencyStore()
        self.payloads = MemoryPayloadStore()
        self.connector = FaultyConnector()
        audit = IdempotencyAudit(AuditTrail(self.audit_store, self.clock), self.diagnostics)
        observer = IdempotencyObserver(self.telemetry, self.events, audit, self.alerts, self.clock)
        self.stage = IdempotencyStage(
            self.store, self.payloads, observer, timing, SeededEntropy(b"idempotency")
        )
        self.maintenance = IdempotencyMaintenance(
            self.store, audit, self.alerts, self.clock, timing
        )

    async def call(
        self, ctx: CallContext | None = None, arguments: JsonObject | None = None
    ) -> ToolResult:
        return await self.stage(
            ctx or context(),
            ToolRequest(arguments=ARGS if arguments is None else arguments),
            self.connector,
        )

    def audited(self) -> list[tuple[object, object]]:
        """(from_state, to_state) of every audited idempotency change, in order."""
        return [
            (r.event.detail["from_state"], r.event.detail["to_state"])
            for chain in self.audit_store.records.values()
            for r in chain
        ]
