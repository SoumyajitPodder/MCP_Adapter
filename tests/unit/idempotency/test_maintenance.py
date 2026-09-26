import logging

import pytest

from adapter_kernel.errors import ErrorCode
from adapter_kernel.pipeline import ToolSuccess
from adapter_verify.idempotency.adapters.alerts import LogOwnerAlerts
from adapter_verify.idempotency.domain.records import AlertKind, IdemState, RecordKey
from adapter_verify.idempotency.fakes import Fault
from adapter_verify.idempotency.service import (
    MANUAL_RESOLUTION_WARNING,
    Operator,
    Resolution,
)
from adapter_verify.observability.domain.audit import AuditKind
from adapter_verify.observability.ports import AuditUnavailableError
from tests.unit.idempotency.support import TOOL, World, context, error, reservation

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

KEY = RecordKey(agent_id="writer", tool=TOOL, key="k-1")
OPERATOR = Operator(name="ops-oncall", os_user="jdoe")


async def _unknown(world: World) -> None:
    world.connector.mode = Fault.SENT_NO_RESPONSE
    await world.call()
    world.connector.mode = Fault.ACK
    assert world.store.records[KEY].state is IdemState.UNKNOWN


async def test_sweep_moves_only_expired_reservations_and_alerts() -> None:
    world = World()
    await world.store.reserve(reservation(world.clock.now()))
    assert await world.maintenance.sweep() == []
    world.clock.advance(30)
    swept = await world.maintenance.sweep()
    assert [r.key for r in swept] == [KEY]
    assert world.store.records[KEY].state is IdemState.UNKNOWN
    assert [k for k, _, _ in world.alerts.alerts] == [AlertKind.LEASE_EXPIRED]
    assert list(world.audit_store.records) == ["system:idempotency-sweeper"]


async def test_sweep_raises_after_the_batch_when_auditing_fails() -> None:
    world = World()
    await world.store.reserve(reservation(world.clock.now()))
    world.clock.advance(31)
    world.audit_store.unavailable = True
    with pytest.raises(AuditUnavailableError):
        await world.maintenance.sweep()
    assert world.store.records[KEY].state is IdemState.UNKNOWN
    assert len(world.alerts.alerts) == 1


async def test_purge_keeps_unknown_rows() -> None:
    world = World()
    await world.call()
    await _unknown_other(world)
    world.clock.advance(72 * 3_600)
    assert await world.maintenance.purge() == 1
    assert [r.key.key for r in await world.maintenance.unknown()] == ["k-2"]


async def test_resolve_as_failed_frees_the_key() -> None:
    world = World()
    await _unknown(world)
    record = await world.maintenance.resolve(
        KEY, Resolution.FAILED, OPERATOR, "backend shows no order"
    )
    assert record is not None
    assert record.state is IdemState.FAILED_RETRYABLE
    chain = world.audit_store.records["operator:ops-oncall"]
    detail = chain[0].event.detail
    assert chain[0].event.kind is AuditKind.MANUAL_RECONCILIATION
    assert (detail["reason"], detail["os_user"], detail["to_state"]) == (
        "backend shows no order",
        "jdoe",
        "FAILED_RETRYABLE",
    )
    assert isinstance((await world.call()).outcome, ToolSuccess)
    assert world.connector.calls == 2


async def test_resolve_as_completed_replays_with_a_warning_and_never_re_executes() -> None:
    world = World()
    await _unknown(world)
    await world.maintenance.resolve(KEY, Resolution.COMPLETED, OPERATOR, "order exists upstream")
    replay = await world.call()
    assert isinstance(replay.outcome, ToolSuccess)
    assert replay.outcome.content == {}
    assert replay.outcome.meta.replayed is True
    assert replay.outcome.meta.warnings == (MANUAL_RESOLUTION_WARNING,)
    assert world.connector.calls == 1


async def test_resolve_rejects_bad_input_and_non_unknown_rows() -> None:
    world = World()
    await world.call()
    with pytest.raises(ValueError, match="reason"):
        await world.maintenance.resolve(KEY, Resolution.FAILED, OPERATOR, "  ")
    with pytest.raises(ValueError, match="operator"):
        await world.maintenance.resolve(
            KEY, Resolution.FAILED, Operator(name="bad name", os_user="x"), "why"
        )
    assert await world.maintenance.resolve(KEY, Resolution.FAILED, OPERATOR, "why") is None
    assert world.store.records[KEY].state is IdemState.COMPLETED


async def test_resolve_raises_when_the_audit_write_fails() -> None:
    world = World()
    await _unknown(world)
    world.audit_store.unavailable = True
    with pytest.raises(AuditUnavailableError):
        await world.maintenance.resolve(KEY, Resolution.COMPLETED, OPERATOR, "why")


async def test_unknown_lists_oldest_first() -> None:
    world = World()
    await _unknown(world)
    world.clock.advance(1)
    await _unknown_other(world)
    assert [r.key.key for r in await world.maintenance.unknown()] == ["k-1", "k-2"]
    assert error(await world.call()) is ErrorCode.RECONCILIATION_PENDING


async def test_log_alerts_carry_identifiers_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    LogOwnerAlerts(logging.getLogger("t.alerts")).alert(AlertKind.OUTCOME_UNKNOWN, KEY, "c-1")
    (record,) = caplog.records
    assert record.getMessage() == "idempotency_alert"
    assert record.__dict__["event"] == {
        "alert": "outcome_unknown",
        "agent_id": "writer",
        "tool": TOOL,
    }
    assert record.__dict__["correlation_id"] == "c-1"


async def _unknown_other(world: World) -> None:
    world.connector.mode = Fault.SENT_NO_RESPONSE
    await world.call(context("k-2"))
    world.connector.mode = Fault.ACK
