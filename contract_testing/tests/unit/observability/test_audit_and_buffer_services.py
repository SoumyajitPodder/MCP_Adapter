import asyncio
from datetime import UTC, datetime

import pytest

from adapter_kernel.errors import ErrorCode
from adapter_verify.common.fakes import ManualClock
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.buffered_sink import BufferedEventSink
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.domain.audit import (
    ANCHOR_CHAIN,
    AuditKind,
    PrincipalKind,
    ProblemKind,
)
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.fakes import (
    MemoryAuditStore,
    MemoryDiagnostics,
    MemoryEventStore,
)
from adapter_verify.observability.ports import AuditUnavailableError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_AT = datetime(2026, 5, 1, tzinfo=UTC)


def _trail() -> tuple[AuditTrail, MemoryAuditStore]:
    store = MemoryAuditStore()
    return AuditTrail(store, ManualClock(_AT)), store


async def _record(trail: AuditTrail, agent: str = "a1") -> None:
    await trail.record(
        principal_kind=PrincipalKind.AGENT,
        principal=agent,
        kind=AuditKind.ACCESS_DECISION,
        correlation_id="c",
        detail={"rule_id": "r1"},
    )


async def test_records_land_in_per_principal_chains() -> None:
    trail, store = _trail()
    await _record(trail, "a1")
    await _record(trail, "a1")
    await _record(trail, "a2")
    assert {k: len(v) for k, v in store.records.items()} == {"agent:a1": 2, "agent:a2": 1}
    assert store.records["agent:a1"][0].event.actor == "agent:a1"


async def test_anchor_records_moved_heads_only() -> None:
    trail, store = _trail()
    await _record(trail, "a1")
    await _record(trail, "a2")
    assert await trail.anchor() == 2
    assert await trail.anchor() == 0
    await _record(trail, "a1")
    assert await trail.anchor() == 1
    assert len(store.records[ANCHOR_CHAIN]) == 3


async def test_verify_detects_tampering_through_the_store() -> None:
    trail, store = _trail()
    for _ in range(3):
        await _record(trail)
    await trail.anchor()
    assert await trail.verify() == []
    del store.records["agent:a1"][1:]
    kinds = {p.kind for p in await trail.verify()}
    assert kinds == {ProblemKind.TRUNCATED_BEHIND_ANCHOR}


async def test_anchor_kind_is_reserved() -> None:
    trail, _ = _trail()
    with pytest.raises(ValueError, match="anchor"):
        await trail.record(
            principal_kind=PrincipalKind.SYSTEM,
            principal="x",
            kind=AuditKind.ANCHOR,
            correlation_id=None,
            detail={},
        )


async def test_unavailable_store_surfaces_to_the_caller() -> None:
    trail, store = _trail()
    store.unavailable = True
    with pytest.raises(AuditUnavailableError):
        await _record(trail)


def _event(n: int, *, critical: bool = False) -> CallEvent:
    return CallEvent(
        timestamp=_AT,
        trace_id="t",
        span_id=f"s{n}",
        correlation_id=f"c{n}",
        agent_id=None,
        tool="order.get",
        semantic_version=None,
        stage=SpanName.TOOL_CALL,
        outcome=Outcome.ERROR if critical else Outcome.OK,
        error_code=ErrorCode.INTERNAL if critical else None,
    )


def _buffer(
    capacity: int = 4, batch: int = 2
) -> tuple[BufferedEventSink, MemoryEventStore, MemoryDiagnostics]:
    store, diagnostics = MemoryEventStore(), MemoryDiagnostics()
    return (
        BufferedEventSink(store, diagnostics, capacity=capacity, batch_size=batch),
        store,
        diagnostics,
    )


async def test_flush_writes_critical_first_in_batches() -> None:
    sink, store, _ = _buffer(capacity=10, batch=2)
    sink.emit(_event(1))
    sink.emit(_event(2, critical=True))
    sink.emit(_event(3))
    await sink.flush()
    assert [e.span_id for e in store.events] == ["s2", "s1", "s3"]
    assert sink.pending == 0


async def test_overflow_evicts_sampled_before_critical() -> None:
    sink, store, diagnostics = _buffer(capacity=2)
    sink.emit(_event(1))
    sink.emit(_event(2, critical=True))
    sink.emit(_event(3, critical=True))
    sink.emit(_event(4))
    assert sink.dropped_sampled == 2
    sink.emit(_event(5, critical=True))
    sink.emit(_event(6, critical=True))
    assert sink.dropped_critical == 2
    assert len(diagnostics.internal_errors) == 1
    await sink.flush()
    assert [e.span_id for e in store.events] == ["s2", "s3"]


async def test_write_failure_keeps_critical_events_for_the_next_flush() -> None:
    sink, store, diagnostics = _buffer(capacity=10, batch=10)
    sink.emit(_event(1))
    sink.emit(_event(2, critical=True))
    store.fail_next = 1
    await sink.flush()
    assert store.events == []
    assert sink.pending == 1
    assert sink.dropped_sampled == 1
    assert "ConnectionError" in diagnostics.internal_errors[0][1]
    assert "simulated" not in diagnostics.internal_errors[0][1]
    await sink.flush()
    assert [e.span_id for e in store.events] == ["s2"]


async def test_requeue_beyond_capacity_drops_critical_with_an_alert() -> None:
    sink, store, _ = _buffer(capacity=2, batch=2)
    sink.emit(_event(1, critical=True))
    sink.emit(_event(2, critical=True))
    store.fail_next = 1
    flushing = asyncio.create_task(sink.flush())
    await asyncio.sleep(0)
    sink.emit(_event(3, critical=True))
    await flushing
    assert sink.pending == 2
    assert sink.dropped_critical == 1


async def test_run_flushes_until_cancelled_then_drains() -> None:
    sink, store, _ = _buffer(capacity=10)
    task = asyncio.create_task(sink.run(0.001))
    sink.emit(_event(1))
    await asyncio.sleep(0.01)
    assert len(store.events) == 1
    sink.emit(_event(2, critical=True))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.events) == 2


async def test_run_retries_a_failed_write_without_a_new_event() -> None:
    sink, store, _ = _buffer(capacity=10)
    store.fail_next = 1
    task = asyncio.create_task(sink.run(0.001))
    sink.emit(_event(1, critical=True))
    for _ in range(100):
        await asyncio.sleep(0.001)
        if store.events:
            break
    written = [e.span_id for e in store.events]  # before cancelling, which drains anyway
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written == ["s1"]


async def test_buffer_rejects_nonsense_sizes() -> None:
    with pytest.raises(ValueError, match="positive"):
        BufferedEventSink(MemoryEventStore(), MemoryDiagnostics(), capacity=0, batch_size=1)
