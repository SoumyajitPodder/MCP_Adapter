from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from adapter_verify import composition
from adapter_verify.cli.main import cli
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.fakes import MemoryEventStore

pytestmark = pytest.mark.unit

FAKE_DSN = "postgresql://user:SENTINEL-db-password@db.invalid:5432/adapter"


class _Pool:
    async def close(self) -> None:
        return None


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> MemoryEventStore:
    store = MemoryEventStore()

    async def open_pool(settings: object) -> _Pool:
        del settings
        return _Pool()

    monkeypatch.setenv("ADAPTER_DATABASE_DSN", FAKE_DSN)
    monkeypatch.setattr(composition, "open_pool", open_pool)
    monkeypatch.setattr(composition, "trace_query", lambda _pool: store)
    return store


def test_missing_dsn_fails_with_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAPTER_DATABASE_DSN", raising=False)
    result = CliRunner().invoke(cli, ["trace", "--correlation-id", "c"])
    assert result.exit_code == 3
    assert "dsn" in result.output


def test_invalid_dsn_is_reported_without_echoing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_DATABASE_DSN", "mysql://user:SENTINEL-db-password@x/y")
    result = CliRunner().invoke(cli, ["audit", "verify"])
    assert result.exit_code == 3
    assert "SENTINEL" not in result.output


def test_invalid_correlation_id_is_rejected(fake_db: MemoryEventStore) -> None:
    del fake_db
    result = CliRunner().invoke(cli, ["trace", "--correlation-id", "bad id"])
    assert result.exit_code == 3


def test_trace_with_no_events_exits_1(fake_db: MemoryEventStore) -> None:
    del fake_db
    result = CliRunner().invoke(cli, ["trace", "--correlation-id", "c"])
    assert result.exit_code == 1


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_trace_prints_events(fake_db: MemoryEventStore, fmt: str) -> None:
    fake_db.events.append(
        CallEvent(
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            trace_id="t",
            span_id="s",
            correlation_id="c-1",
            agent_id=None,
            tool="order.get",
            semantic_version=None,
            stage=SpanName.TOOL_CALL,
            outcome=Outcome.OK,
        )
    )
    result = CliRunner().invoke(cli, ["trace", "--correlation-id", "c-1", "--format", fmt])
    assert result.exit_code == 0
    assert "tool.call" in result.output
    assert "SENTINEL" not in result.output


def test_database_errors_are_reported_by_type_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_pool(settings: object) -> _Pool:
        del settings
        raise OSError("connect failed to host SENTINEL-internal-host")

    monkeypatch.setenv("ADAPTER_DATABASE_DSN", FAKE_DSN)
    monkeypatch.setattr(composition, "open_pool", open_pool)
    result = CliRunner().invoke(cli, ["audit", "anchor"])
    assert result.exit_code == 3
    assert "OSError" in result.output
    assert "SENTINEL" not in result.output
