"""Port contract suites: every adapter of a port must pass the same tests.

Each suite takes a factory so the production adapter can be added next to the fake once it
exists (the payload store's real adapter is pending brief §15).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_verify.idempotency.domain.records import (
    IdemState,
    RecordKey,
    Reservation,
    Transition,
)
from adapter_verify.idempotency.ports import IdempotencyStore
from adapter_verify.observability.ports import PayloadKind, PayloadStore


async def payload_store_contract(make: Callable[[], Awaitable[PayloadStore]]) -> None:
    store = await make()
    body: JsonObject = {"order_id": "88213", "items": [{"sku": "SKU-1"}], "note": None}
    ref = await store.put("c-1", PayloadKind.RESULT, body)
    assert isinstance(ref, str)
    assert "88213" not in ref
    assert await store.get(ref) == body

    other = await store.put("c-1", PayloadKind.RESULT, body)
    assert other != ref

    body["order_id"] = "changed-after-put"
    assert await store.get(ref) != body

    with pytest.raises(KeyError):
        await store.get("payload:does-not-exist")


_T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _reservation(key: str = "k-1", *, fp: bytes = b"\x01" * 32, lease_s: int = 30) -> Reservation:
    return Reservation(
        key=RecordKey(agent_id="writer", tool="order.cancel", key=key),
        fingerprint=fp,
        semantic_version="1.0.0",
        attempt_id=uuid4(),
        correlation_id="c-1",
        at=_T0,
        lease_expires_at=_T0 + timedelta(seconds=lease_s),
        expires_at=_T0 + timedelta(hours=72),
    )


def _transition(r: Reservation, to: IdemState, *states: IdemState, **kw: Any) -> Transition:
    return Transition(
        key=r.key,
        attempt_id=r.attempt_id,
        from_states=frozenset(states or {IdemState.RESERVED}),
        to_state=to,
        at=_T0 + timedelta(seconds=1),
        **kw,
    )


async def idempotency_store_contract(  # noqa: PLR0915 - one scenario, in order
    make: Callable[[], Awaitable[IdempotencyStore]],
) -> None:
    store = await make()
    first = _reservation()

    # Reserve is exactly-once; the loser sees the holder's record.
    assert await store.reserve(first) is None
    held = await store.reserve(_reservation())
    assert held is not None
    assert held.state is IdemState.RESERVED
    assert held.attempt_id == first.attempt_id
    assert held.fingerprint == first.fingerprint
    assert held.lease_expires_at == first.lease_expires_at
    assert await store.get(first.key) == held

    # Transitions are fenced by attempt and state, and report the state they left.
    stranger = first.model_copy(update={"attempt_id": uuid4()})
    assert await store.transition(_transition(stranger, IdemState.COMPLETED)) is None
    assert (
        await store.transition(
            _transition(first, IdemState.COMPLETED, result_ref="payload:1", error_code=None)
        )
        is IdemState.RESERVED
    )
    done = await store.get(first.key)
    assert done is not None
    assert (done.state, done.result_ref, done.lease_expires_at) == (
        IdemState.COMPLETED,
        "payload:1",
        None,
    )
    assert await store.transition(_transition(first, IdemState.UNKNOWN)) is None

    # Re-reserve only a FAILED_RETRYABLE row with the same fingerprint; rotates the attempt.
    failed = _reservation("k-2")
    assert await store.reserve(failed) is None
    await store.transition(
        _transition(failed, IdemState.FAILED_RETRYABLE, error_code=ErrorCode.INTERNAL)
    )
    assert not await store.rereserve(_reservation("k-2", fp=b"\x02" * 32))
    again = _reservation("k-2")
    assert await store.rereserve(again)
    assert not await store.rereserve(_reservation("k-2"))
    row = await store.get(again.key)
    assert row is not None
    assert (row.state, row.attempt_id, row.error_code) == (
        IdemState.RESERVED,
        again.attempt_id,
        None,
    )
    assert not await store.rereserve(_reservation("missing"))

    # Leases expire into UNKNOWN; only expired RESERVED rows move.
    assert await store.expire_leases(_T0 + timedelta(seconds=29), 10) == []
    expired = await store.expire_leases(_T0 + timedelta(seconds=30), 10)
    assert [r.key.key for r in expired] == ["k-2"]
    assert expired[0].state is IdemState.UNKNOWN
    assert [r.key.key for r in await store.unknown(10)] == ["k-2"]

    # A late ACKED completion may still move UNKNOWN to COMPLETED with the matching attempt.
    late = _transition(again, IdemState.COMPLETED, IdemState.RESERVED, IdemState.UNKNOWN)
    assert await store.transition(late) is IdemState.UNKNOWN

    # Manual resolution only applies to UNKNOWN rows and restarts retention.
    pending = _reservation("k-3", lease_s=1)
    await store.reserve(pending)
    await store.expire_leases(_T0 + timedelta(seconds=5), 10)
    later = _T0 + timedelta(hours=100)
    resolved = await store.resolve(
        pending.key, IdemState.FAILED_RETRYABLE, later, later + timedelta(hours=72)
    )
    assert resolved is not None
    assert (resolved.state, resolved.expires_at) == (
        IdemState.FAILED_RETRYABLE,
        later + timedelta(hours=72),
    )
    assert await store.resolve(pending.key, IdemState.COMPLETED, later, later) is None
    assert (
        await store.resolve(_reservation("missing").key, IdemState.COMPLETED, later, later) is None
    )

    # Purge removes expired COMPLETED/FAILED_RETRYABLE rows, never UNKNOWN ones.
    stuck = _reservation("k-4", lease_s=1)
    await store.reserve(stuck)
    await store.expire_leases(_T0 + timedelta(seconds=5), 10)
    assert await store.purge(_T0 + timedelta(hours=1), 10) == 0
    assert await store.purge(_T0 + timedelta(hours=73), 10) == 2  # k-1 and k-2
    assert await store.get(stuck.key) is not None
    assert await store.get(pending.key) is not None  # retention restarted by resolve
    assert await store.purge(later + timedelta(hours=73), 10) == 1
    assert [r.key.key for r in await store.unknown(10)] == ["k-4"]
