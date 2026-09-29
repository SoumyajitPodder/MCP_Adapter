"""Brief §7.10 acceptance on real Postgres: N concurrent identical calls make exactly one
upstream call, and key reuse with different arguments is rejected. The crash case is in
``test_idempotency_crash.py``."""

import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest

from adapter_kernel.context import CallContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.pipeline import ToolFailure, ToolRequest, ToolResult, ToolSuccess
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.idempotency.adapters.postgres import PostgresIdempotencyStore
from adapter_verify.idempotency.domain.records import RecordKey
from adapter_verify.idempotency.fakes import FaultyConnector
from adapter_verify.idempotency.ports import IdempotencyStore, IdempotencyStoreUnavailableError
from tests.contracts import idempotency_store_contract
from tests.integration import idempotency_support
from tests.unit.idempotency.support import ARGS, context

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CALLS = 50


async def test_postgres_store_meets_the_contract(pool: asyncpg.Pool) -> None:
    async def make() -> IdempotencyStore:
        return PostgresIdempotencyStore(pool)

    await idempotency_store_contract(make)


async def test_concurrent_identical_calls_make_exactly_one_upstream_call(
    pool: asyncpg.Pool,
) -> None:
    clock = ManualClock(datetime(2026, 9, 25, tzinfo=UTC))
    stage = idempotency_support.stage(pool, clock, SeededEntropy(b"concurrency"))
    backend = FaultyConnector()

    async def slow_backend(ctx: CallContext, request: ToolRequest) -> ToolResult:
        await asyncio.sleep(0.05)
        return await backend(ctx, request)

    results = await asyncio.gather(
        *(
            stage(context(cid=f"c-{i}"), ToolRequest(arguments=ARGS), slow_backend)
            for i in range(CALLS)
        )
    )

    assert backend.calls == 1
    codes = {r.outcome.error.code for r in results if isinstance(r.outcome, ToolFailure)}
    assert codes <= {ErrorCode.DUPLICATE_IN_PROGRESS}
    successes = [r.outcome for r in results if isinstance(r.outcome, ToolSuccess)]
    assert successes
    assert all(s.content == {"effect": 1} for s in successes)
    row = await pool.fetchrow("SELECT state, result_ref FROM idempotency_records")
    assert row is not None
    assert row["state"] == "COMPLETED"
    assert row["result_ref"] is not None

    # Afterwards every retry is a replay.
    replay = await stage(context(cid="c-late"), ToolRequest(arguments=ARGS), slow_backend)
    assert isinstance(replay.outcome, ToolSuccess)
    assert replay.outcome.meta.replayed is True
    assert backend.calls == 1


async def test_key_reuse_with_different_arguments_is_rejected(pool: asyncpg.Pool) -> None:
    clock = ManualClock(datetime(2026, 9, 25, tzinfo=UTC))
    stage = idempotency_support.stage(pool, clock, SeededEntropy(b"conflict"))
    backend = FaultyConnector()
    await stage(context(), ToolRequest(arguments=ARGS), backend)
    other = await stage(context(), ToolRequest(arguments={**ARGS, "order_id": "o-2"}), backend)
    assert isinstance(other.outcome, ToolFailure)
    assert other.outcome.error.code is ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    assert backend.calls == 1
    chain = await pool.fetch(
        "SELECT event->'detail'->>'to_state' AS to_state FROM audit_log "
        "WHERE chain_id = 'agent:writer' ORDER BY seq"
    )
    assert [r["to_state"] for r in chain] == ["RESERVED", "COMPLETED"]


async def test_database_errors_surface_as_store_unavailable(database_url: str) -> None:
    closed = await asyncpg.create_pool(database_url, min_size=1, max_size=1)
    store = PostgresIdempotencyStore(closed)
    await closed.close()
    with pytest.raises(IdempotencyStoreUnavailableError):
        await store.get(RecordKey(agent_id="writer", tool="order.cancel", key="k"))
