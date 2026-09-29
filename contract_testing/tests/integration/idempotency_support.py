"""The idempotency stage wired to real Postgres stores, with in-memory telemetry."""

import asyncpg

from adapter_verify.common.ports import Clock, Entropy
from adapter_verify.idempotency.adapters.postgres import PostgresIdempotencyStore
from adapter_verify.idempotency.domain.records import Timing
from adapter_verify.idempotency.fakes import MemoryOwnerAlerts
from adapter_verify.idempotency.service import (
    IdempotencyAudit,
    IdempotencyMaintenance,
    IdempotencyObserver,
    IdempotencyStage,
)
from adapter_verify.observability.adapters.postgres import PostgresAuditStore
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.fakes import (
    MemoryDiagnostics,
    MemoryEventSink,
    MemoryPayloadStore,
    RecordingTelemetry,
)

TIMING = Timing(lease_s=30, retention_h=72)


def audit(pool: asyncpg.Pool, clock: Clock) -> IdempotencyAudit:
    return IdempotencyAudit(AuditTrail(PostgresAuditStore(pool), clock), MemoryDiagnostics())


def stage(pool: asyncpg.Pool, clock: Clock, entropy: Entropy) -> IdempotencyStage:
    observer = IdempotencyObserver(
        RecordingTelemetry(), MemoryEventSink(), audit(pool, clock), MemoryOwnerAlerts(), clock
    )
    return IdempotencyStage(
        PostgresIdempotencyStore(pool), MemoryPayloadStore(), observer, TIMING, entropy
    )


def maintenance(pool: asyncpg.Pool, clock: Clock) -> IdempotencyMaintenance:
    return IdempotencyMaintenance(
        PostgresIdempotencyStore(pool), audit(pool, clock), MemoryOwnerAlerts(), clock, TIMING
    )
