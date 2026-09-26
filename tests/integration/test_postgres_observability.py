"""Postgres adapters for §8: migrations, event store, audit store, CLI (brief §8.10)."""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from adapter_verify.common.adapters.postgres import load_migrations, migrate
from adapter_verify.common.domain.migrations import MigrationError, parse
from adapter_verify.common.fakes import ManualClock
from adapter_verify.composition import MIGRATIONS_DIR
from adapter_verify.observability.adapters.postgres import PostgresAuditStore, PostgresEventStore
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.domain.audit import AuditKind, PrincipalKind, ProblemKind
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.ports import AuditUnavailableError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


async def test_migrations_are_idempotent_and_refuse_edits(pool: asyncpg.Pool) -> None:
    migrations = load_migrations(MIGRATIONS_DIR)
    assert await migrate(pool, migrations) == []
    edited = [parse(migrations[0].name, migrations[0].sql + "\n-- edited"), *migrations[1:]]
    with pytest.raises(MigrationError, match="edited after it ran"):
        await migrate(pool, edited)


def _event(cid: str, stage: SpanName, offset_ms: int) -> CallEvent:
    return CallEvent(
        timestamp=_AT + timedelta(milliseconds=offset_ms),
        trace_id="t",
        span_id=f"s{offset_ms}",
        correlation_id=cid,
        agent_id="synthetic-agent",
        tool="order.get",
        semantic_version="1.2.0",
        stage=stage,
        outcome=Outcome.OK,
    )


async def test_event_store_returns_one_correlation_in_time_order(pool: asyncpg.Pool) -> None:
    store = PostgresEventStore(pool)
    await store.write(
        [
            _event("c-1", SpanName.BACKEND_CALL, 30),
            _event("c-2", SpanName.TOOL_CALL, 5),
            _event("c-1", SpanName.ACCESS_CHECK, 10),
            _event("c-1", SpanName.TOOL_CALL, 40),
        ]
    )
    found = await store.by_correlation_id("c-1")
    assert [e.stage for e in found] == [
        SpanName.ACCESS_CHECK,
        SpanName.BACKEND_CALL,
        SpanName.TOOL_CALL,
    ]
    assert found[0] == _event("c-1", SpanName.ACCESS_CHECK, 10)


async def _record(trail: AuditTrail, agent: str, n: int) -> None:
    await trail.record(
        principal_kind=PrincipalKind.AGENT,
        principal=agent,
        kind=AuditKind.ACCESS_DECISION,
        correlation_id=f"c-{n}",
        detail={"rule_id": f"r{n}", "allowed": True},
    )


async def test_concurrent_appends_produce_one_gapless_chain(pool: asyncpg.Pool) -> None:
    trail = AuditTrail(PostgresAuditStore(pool), ManualClock(_AT))
    await asyncio.gather(*(_record(trail, "a1", n) for n in range(40)))
    records = await PostgresAuditStore(pool).read_chain("agent:a1")
    assert [r.seq for r in records] == list(range(1, 41))
    assert await trail.verify() == []


async def test_audit_table_is_append_only(pool: asyncpg.Pool) -> None:
    await _record(AuditTrail(PostgresAuditStore(pool), ManualClock(_AT)), "a1", 1)
    for statement in (
        "UPDATE audit_log SET event = '{}'::jsonb",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await pool.execute(statement)


async def test_tampering_by_a_privileged_user_is_detected(pool: asyncpg.Pool) -> None:
    trail = AuditTrail(PostgresAuditStore(pool), ManualClock(_AT))
    for n in range(4):
        await _record(trail, "a1", n)
    await _record(trail, "a2", 0)
    assert await trail.anchor() == 2
    assert await trail.verify() == []

    # Simulate someone able to bypass the append-only trigger (e.g. a superuser).
    await pool.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
    await pool.execute(
        "UPDATE audit_log SET event = jsonb_set(event, '{detail,allowed}', 'false') "
        "WHERE chain_id = 'agent:a1' AND seq = 2"
    )
    await pool.execute("DELETE FROM audit_log WHERE chain_id = 'agent:a2'")

    problems = {(p.kind, p.chain_id, p.seq) for p in await trail.verify()}
    assert (ProblemKind.HASH_MISMATCH, "agent:a1", 2) in problems
    assert (ProblemKind.TRUNCATED_BEHIND_ANCHOR, "agent:a2", 1) in problems


async def test_store_errors_carry_no_detail(pool: asyncpg.Pool) -> None:
    store = PostgresAuditStore(pool)
    await pool.execute("ALTER TABLE audit_log RENAME TO audit_log_gone")
    trail = AuditTrail(store, ManualClock(_AT))
    with pytest.raises(AuditUnavailableError) as caught:
        await _record(trail, "a1", 1)
    assert str(caught.value) == ""
    assert caught.value.__cause__ is None
