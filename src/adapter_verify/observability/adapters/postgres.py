"""Postgres implementations of the event store, trace query and audit store (asyncpg).

Database errors are converted to port-level errors with no message: driver messages can
quote parameters, and those can contain customer data.
"""

from collections.abc import Sequence
from typing import Final

import asyncpg

from adapter_verify.observability.domain.audit import AuditEvent, AuditRecord, ChainHead, link
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.ports import AuditUnavailableError

_DB_ERRORS: Final = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class PostgresEventStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write(self, events: Sequence[CallEvent]) -> None:
        await self._pool.executemany(
            "INSERT INTO call_events (correlation_id, occurred_at, schema_version, event) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            [
                (e.correlation_id, e.timestamp, e.schema_version, e.model_dump_json())
                for e in events
            ],
        )

    async def by_correlation_id(self, correlation_id: str) -> list[CallEvent]:
        rows = await self._pool.fetch(
            "SELECT event::text AS event FROM call_events WHERE correlation_id = $1 "
            "ORDER BY occurred_at, id",
            correlation_id,
        )
        return [CallEvent.model_validate_json(r["event"]) for r in rows]


class PostgresAuditStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def append(self, chain_id: str, event: AuditEvent) -> AuditRecord:
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                # Serializes appends per chain only (R-011), across all adapter instances.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", chain_id
                )
                row = await conn.fetchrow(
                    "SELECT seq, record_hash FROM audit_log WHERE chain_id = $1 "
                    "ORDER BY seq DESC LIMIT 1",
                    chain_id,
                )
                head = (
                    None
                    if row is None
                    else ChainHead(
                        chain_id=chain_id, seq=row["seq"], record_hash=row["record_hash"]
                    )
                )
                record = link(chain_id, head, event)
                await conn.execute(
                    "INSERT INTO audit_log (chain_id, seq, prev_hash, record_hash, event) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb)",
                    record.chain_id,
                    record.seq,
                    record.prev_hash,
                    record.record_hash,
                    record.event.model_dump_json(),
                )
        except _DB_ERRORS:
            raise AuditUnavailableError from None
        return record

    async def read_chain(self, chain_id: str) -> list[AuditRecord]:
        rows = await self._pool.fetch(
            "SELECT chain_id, seq, prev_hash, record_hash, event::text AS event "
            "FROM audit_log WHERE chain_id = $1 ORDER BY seq",
            chain_id,
        )
        return [
            AuditRecord(
                chain_id=r["chain_id"],
                seq=r["seq"],
                prev_hash=r["prev_hash"],
                record_hash=r["record_hash"],
                event=AuditEvent.model_validate_json(r["event"]),
            )
            for r in rows
        ]

    async def heads(self) -> list[ChainHead]:
        rows = await self._pool.fetch(
            "SELECT DISTINCT ON (chain_id) chain_id, seq, record_hash FROM audit_log "
            "ORDER BY chain_id, seq DESC"
        )
        return [
            ChainHead(chain_id=r["chain_id"], seq=r["seq"], record_hash=r["record_hash"])
            for r in rows
        ]
