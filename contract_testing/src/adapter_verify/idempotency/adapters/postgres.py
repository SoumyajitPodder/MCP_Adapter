"""Postgres idempotency store (asyncpg). Every write is a single conditional statement or one
short transaction; the primary key is what makes reservation exactly-once (§7.6).

Driver errors become ``IdempotencyStoreUnavailableError`` with no message: drivers can quote
parameters.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final
from uuid import UUID

import asyncpg

from adapter_kernel.errors import ErrorCode
from adapter_verify.idempotency.domain.records import (
    IdempotencyRecord,
    IdemState,
    RecordKey,
    Reservation,
    Transition,
)
from adapter_verify.idempotency.ports import IdempotencyStoreUnavailableError

_DB_ERRORS: Final = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)
_RESERVE_ATTEMPTS: Final = 3


def _record(row: asyncpg.Record) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=RecordKey(agent_id=row["agent_id"], tool=row["tool"], key=row["idem_key"]),
        fingerprint=bytes(row["fingerprint"]),
        state=IdemState(row["state"]),
        semantic_version=row["semantic_version"],
        attempt_id=UUID(int=row["attempt_id"].int),
        correlation_id=row["correlation_id"],
        result_ref=row["result_ref"],
        error_code=None if row["error_code"] is None else ErrorCode(row["error_code"]),
        lease_expires_at=row["lease_expires_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _affected(status: str) -> int:
    return int(status.rsplit(" ", 1)[-1])


async def _guard[T](work: Callable[[], Awaitable[T]]) -> T:
    try:
        return await work()
    except _DB_ERRORS:
        raise IdempotencyStoreUnavailableError from None


class PostgresIdempotencyStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def reserve(self, reservation: Reservation) -> IdempotencyRecord | None:
        r, k = reservation, reservation.key

        async def work() -> IdempotencyRecord | None:
            for _ in range(_RESERVE_ATTEMPTS):
                inserted = await self._pool.fetchval(
                    "INSERT INTO idempotency_records (agent_id, tool, idem_key, fingerprint, "
                    "state, "
                    "semantic_version, attempt_id, correlation_id, result_ref, error_code, "
                    "lease_expires_at, expires_at, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, 'RESERVED', $5, $6, $7, NULL, NULL, $8, $9, $10, $10) "
                    "ON CONFLICT (agent_id, tool, idem_key) DO NOTHING RETURNING true",
                    k.agent_id,
                    k.tool,
                    k.key,
                    r.fingerprint,
                    r.semantic_version,
                    r.attempt_id,
                    r.correlation_id,
                    r.lease_expires_at,
                    r.expires_at,
                    r.at,
                )
                if inserted:
                    return None
                existing = await self.get(k)
                if existing is not None:
                    return existing
                # Purged between the conflict and the read: try the insert again.
            raise IdempotencyStoreUnavailableError

        return await _guard(work)

    async def rereserve(self, reservation: Reservation) -> bool:
        r, k = reservation, reservation.key
        status = await _guard(
            lambda: self._pool.execute(
                "UPDATE idempotency_records SET state = 'RESERVED', attempt_id = $6, "
                "correlation_id = $7, result_ref = NULL, error_code = NULL, "
                "lease_expires_at = $8, expires_at = $9, updated_at = $10 "
                "WHERE agent_id = $1 AND tool = $2 AND idem_key = $3 "
                "AND state = 'FAILED_RETRYABLE' "
                "AND fingerprint = $4 AND semantic_version = $5",
                k.agent_id,
                k.tool,
                k.key,
                r.fingerprint,
                r.semantic_version,
                r.attempt_id,
                r.correlation_id,
                r.lease_expires_at,
                r.expires_at,
                r.at,
            )
        )
        return _affected(status) == 1

    async def get(self, key: RecordKey) -> IdempotencyRecord | None:
        row = await _guard(
            lambda: self._pool.fetchrow(
                "SELECT * FROM idempotency_records "
                "WHERE agent_id = $1 AND tool = $2 AND idem_key = $3",
                key.agent_id,
                key.tool,
                key.key,
            )
        )
        return None if row is None else _record(row)

    async def transition(self, transition: Transition) -> IdemState | None:
        t, k = transition, transition.key

        async def work() -> IdemState | None:
            async with self._pool.acquire() as conn, conn.transaction():
                previous = await conn.fetchval(
                    "SELECT state FROM idempotency_records "
                    "WHERE agent_id = $1 AND tool = $2 AND idem_key = $3 "
                    "AND attempt_id = $4 AND state = ANY($5::text[]) FOR UPDATE",
                    k.agent_id,
                    k.tool,
                    k.key,
                    t.attempt_id,
                    sorted(s.value for s in t.from_states),
                )
                if previous is None:
                    return None
                await conn.execute(
                    "UPDATE idempotency_records SET state = $4, result_ref = $5, "
                    "error_code = $6, lease_expires_at = NULL, updated_at = $7 "
                    "WHERE agent_id = $1 AND tool = $2 AND idem_key = $3",
                    k.agent_id,
                    k.tool,
                    k.key,
                    t.to_state.value,
                    t.result_ref,
                    None if t.error_code is None else t.error_code.value,
                    t.at,
                )
                return IdemState(previous)

        return await _guard(work)

    async def expire_leases(self, now: datetime, limit: int) -> list[IdempotencyRecord]:
        rows = await _guard(
            lambda: self._pool.fetch(
                "UPDATE idempotency_records SET state = 'UNKNOWN', updated_at = $1 "
                "WHERE (agent_id, tool, idem_key) IN ("
                "  SELECT agent_id, tool, idem_key FROM idempotency_records "
                "  WHERE state = 'RESERVED' AND lease_expires_at <= $1 "
                "  ORDER BY lease_expires_at LIMIT $2 FOR UPDATE SKIP LOCKED) "
                "RETURNING *",
                now,
                limit,
            )
        )
        return [_record(r) for r in rows]

    async def purge(self, now: datetime, limit: int) -> int:
        status = await _guard(
            lambda: self._pool.execute(
                "DELETE FROM idempotency_records WHERE (agent_id, tool, idem_key) IN ("
                "  SELECT agent_id, tool, idem_key FROM idempotency_records "
                "  WHERE state IN ('COMPLETED', 'FAILED_RETRYABLE') AND expires_at <= $1 "
                "  LIMIT $2 FOR UPDATE SKIP LOCKED)",
                now,
                limit,
            )
        )
        return _affected(status)

    async def unknown(self, limit: int) -> list[IdempotencyRecord]:
        rows = await _guard(
            lambda: self._pool.fetch(
                "SELECT * FROM idempotency_records WHERE state = 'UNKNOWN' "
                "ORDER BY updated_at LIMIT $1",
                limit,
            )
        )
        return [_record(r) for r in rows]

    async def resolve(
        self, key: RecordKey, to_state: IdemState, now: datetime, expires_at: datetime
    ) -> IdempotencyRecord | None:
        row = await _guard(
            lambda: self._pool.fetchrow(
                "UPDATE idempotency_records SET state = $4, lease_expires_at = NULL, "
                "expires_at = $5, updated_at = $6 "
                "WHERE agent_id = $1 AND tool = $2 AND idem_key = $3 "
                "AND state = 'UNKNOWN' RETURNING *",
                key.agent_id,
                key.tool,
                key.key,
                to_state.value,
                expires_at,
                now,
            )
        )
        return None if row is None else _record(row)
