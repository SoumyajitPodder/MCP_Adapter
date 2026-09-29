"""The adapter-verify CLI against a real database."""

import asyncio
from pathlib import Path

import asyncpg
import pytest
from click.testing import CliRunner

from adapter_verify.cli.main import cli
from adapter_verify.common.fakes import ManualClock
from adapter_verify.observability.adapters.postgres import PostgresAuditStore, PostgresEventStore
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.domain.attributes import SpanName
from tests.integration.test_postgres_observability import _AT, _event, _record

pytestmark = pytest.mark.integration


def test_cli_end_to_end(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_DATABASE_DSN", database_url)
    runner = CliRunner()

    migrated = runner.invoke(cli, ["db", "migrate"])
    assert migrated.exit_code == 0, migrated.output
    assert "0001_call_events.sql" in migrated.output
    assert runner.invoke(cli, ["db", "migrate"]).output.strip() == "up to date"

    async def seed() -> None:
        conn_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            await PostgresEventStore(conn_pool).write([_event("c-9", SpanName.TOOL_CALL, 1)])
            await _record(AuditTrail(PostgresAuditStore(conn_pool), ManualClock(_AT)), "a1", 1)
        finally:
            await conn_pool.close()

    asyncio.run(seed())

    traced = runner.invoke(cli, ["trace", "--correlation-id", "c-9"])
    assert traced.exit_code == 0
    assert "tool.call" in traced.output
    assert runner.invoke(cli, ["audit", "anchor"]).output.strip() == "anchored 1 chain(s)"
    verified = runner.invoke(cli, ["audit", "verify"])
    assert verified.exit_code == 0
    assert "intact" in verified.output


def test_acceptance_tampered_audit_record_fails_verify(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brief §8.10: a tampered audit record is detected, through the operator's own tool."""
    monkeypatch.setenv("ADAPTER_DATABASE_DSN", database_url)
    runner = CliRunner()
    assert runner.invoke(cli, ["db", "migrate"]).exit_code == 0

    async def seed_and_tamper() -> None:
        conn_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            trail = AuditTrail(PostgresAuditStore(conn_pool), ManualClock(_AT))
            for n in range(3):
                await _record(trail, "order-status-agent", n)
            await trail.anchor()
            await conn_pool.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
            await conn_pool.execute(
                "UPDATE audit_log SET event = jsonb_set(event, '{actor}', '\"agent:someone\"') "
                "WHERE chain_id = 'agent:order-status-agent' AND seq = 1"
            )
        finally:
            await conn_pool.close()

    asyncio.run(seed_and_tamper())
    verified = runner.invoke(cli, ["audit", "verify"])
    assert verified.exit_code == 1
    assert "hash_mismatch\tagent:order-status-agent\tseq=1" in verified.output


def test_cli_refuses_a_bad_migration_directory(
    database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ADAPTER_DATABASE_DSN", database_url)
    (tmp_path / "bad-name.sql").write_text("SELECT 1;", encoding="utf-8")
    result = CliRunner().invoke(cli, ["db", "migrate", "--migrations-dir", str(tmp_path)])
    assert result.exit_code == 3
    assert "0001_description.sql" in result.output
