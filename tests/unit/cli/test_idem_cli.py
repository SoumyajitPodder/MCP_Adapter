import asyncio

import pytest
from click.testing import CliRunner

from adapter_verify import composition
from adapter_verify.cli.main import cli
from adapter_verify.idempotency.domain.records import IdemState, RecordKey
from adapter_verify.idempotency.fakes import Fault
from tests.unit.idempotency.support import TOOL, World, context, reservation

pytestmark = pytest.mark.unit

KEY = RecordKey(agent_id="writer", tool=TOOL, key="k-1")


class _Pool:
    async def close(self) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    world = World()

    async def open_pool(settings: object) -> _Pool:
        del settings
        return _Pool()

    monkeypatch.setenv("ADAPTER_DATABASE_DSN", "postgresql://u@db.invalid/adapter")
    monkeypatch.setattr(composition, "open_pool", open_pool)
    monkeypatch.setattr(
        composition, "idempotency_maintenance", lambda _pool, _settings: world.maintenance
    )
    return world


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, ["idem", *args])
    return result.exit_code, result.output


def _unknown(world: World, key: str = "k-1") -> None:
    world.connector.mode = Fault.SENT_NO_RESPONSE
    asyncio.run(world.call(context(key)))


def test_sweep_purge_and_unknown(world: World) -> None:
    asyncio.run(world.store.reserve(reservation(world.clock.now(), "k-9")))
    world.clock.advance(31)
    code, out = _run("sweep")
    assert code == 0
    assert "writer\torder.cancel\tk-9\tUNKNOWN" in out
    assert "1 expired lease(s) marked UNKNOWN" in out

    code, out = _run("unknown")
    assert code == 0
    assert "k-9" in out
    assert "1 row(s) awaiting reconciliation" in out

    code, out = _run("purge")
    assert (code, out.strip()) == (0, "purged 0 row(s)")


def test_resolve_records_the_operator(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _unknown(world)
    monkeypatch.setattr("getpass.getuser", lambda: "jdoe")
    args = ["--agent", "writer", "--tool", TOOL, "--key", "k-1", "--operator", "ops"]
    code, out = _run("resolve", *args, "--as", "failed", "--reason", "no order upstream")
    assert code == 0, out
    assert "FAILED_RETRYABLE" in out
    detail = world.audit_store.records["operator:ops"][0].event.detail
    assert (detail["os_user"], detail["reason"]) == ("jdoe", "no order upstream")
    assert world.store.records[KEY].state is IdemState.FAILED_RETRYABLE

    code, out = _run("resolve", *args, "--as", "completed", "--reason", "again")
    assert code == 1
    assert "no UNKNOWN record" in out


def test_resolve_survives_an_unknown_os_user(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _unknown(world)

    def no_user() -> str:
        raise OSError

    monkeypatch.setattr("getpass.getuser", no_user)
    code, _ = _run(
        "resolve",
        *("--agent", "writer", "--tool", TOOL, "--key", "k-1", "--operator", "ops"),
        *("--as", "completed", "--reason", "order exists"),
    )
    assert code == 0
    assert world.audit_store.records["operator:ops"][0].event.detail["os_user"] == "unknown"


@pytest.mark.parametrize(
    ("key", "operator", "reason"),
    [("bad key", "ops", "r"), ("k-1", "bad operator", "r"), ("k-1", "ops", "   ")],
)
@pytest.mark.usefixtures("world")
def test_resolve_rejects_bad_input(key: str, operator: str, reason: str) -> None:
    code, out = _run(
        "resolve",
        *("--agent", "writer", "--tool", TOOL, "--key", key, "--operator", operator),
        *("--as", "failed", "--reason", reason),
    )
    assert code == 3
    assert "error:" in out


@pytest.mark.usefixtures("world")
def test_invalid_idempotency_settings_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_IDEMPOTENCY_LEASE_S", "0")
    code, out = _run("sweep")
    assert code == 3
    assert "lease_s" in out


def test_audit_outage_during_sweep_exit_3(world: World) -> None:
    asyncio.run(world.store.reserve(reservation(world.clock.now())))
    world.clock.advance(31)
    world.audit_store.unavailable = True
    code, out = _run("sweep")
    assert code == 3
    assert "AuditUnavailableError" in out
