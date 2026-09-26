"""In-memory idempotency ports and a fault-injecting connector (DESIGN.md X-6, X-10)."""

import asyncio
from collections import deque
from datetime import datetime
from enum import StrEnum

from adapter_kernel.context import CallContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_verify.idempotency.domain.records import (
    AlertKind,
    IdempotencyRecord,
    IdemState,
    RecordKey,
    Reservation,
    Transition,
)
from adapter_verify.idempotency.ports import IdempotencyStoreUnavailableError

_PURGEABLE = frozenset({IdemState.COMPLETED, IdemState.FAILED_RETRYABLE})


class MemoryIdempotencyStore:
    """IdempotencyStore in memory. Each method is atomic: none awaits while holding state.

    Set ``unavailable`` to simulate an outage of every call, or ``fail_transitions`` to fail
    only settlement writes.
    """

    def __init__(self) -> None:
        self.records: dict[RecordKey, IdempotencyRecord] = {}
        self.unavailable = False
        self.fail_transitions = False

    def _check(self) -> None:
        if self.unavailable:
            raise IdempotencyStoreUnavailableError

    async def reserve(self, reservation: Reservation) -> IdempotencyRecord | None:
        self._check()
        existing = self.records.get(reservation.key)
        if existing is not None:
            return existing
        self.records[reservation.key] = IdempotencyRecord.reserved(reservation)
        return None

    async def rereserve(self, reservation: Reservation) -> bool:
        self._check()
        row = self.records.get(reservation.key)
        if (
            row is None
            or row.state is not IdemState.FAILED_RETRYABLE
            or row.fingerprint != reservation.fingerprint
            or row.semantic_version != reservation.semantic_version
        ):
            return False
        self.records[reservation.key] = IdempotencyRecord.reserved(reservation).model_copy(
            update={"created_at": row.created_at}
        )
        return True

    async def get(self, key: RecordKey) -> IdempotencyRecord | None:
        self._check()
        return self.records.get(key)

    async def transition(self, transition: Transition) -> IdemState | None:
        self._check()
        if self.fail_transitions:
            raise IdempotencyStoreUnavailableError
        row = self.records.get(transition.key)
        if (
            row is None
            or row.attempt_id != transition.attempt_id
            or row.state not in transition.from_states
        ):
            return None
        self.records[transition.key] = row.model_copy(
            update={
                "state": transition.to_state,
                "result_ref": transition.result_ref,
                "error_code": transition.error_code,
                "lease_expires_at": None,
                "updated_at": transition.at,
            }
        )
        return row.state

    async def expire_leases(self, now: datetime, limit: int) -> list[IdempotencyRecord]:
        self._check()
        expired = sorted(
            (
                r
                for r in self.records.values()
                if r.state is IdemState.RESERVED
                and r.lease_expires_at is not None
                and r.lease_expires_at <= now
            ),
            key=lambda r: r.lease_expires_at or now,
        )[:limit]
        updated = []
        for row in expired:
            new = row.model_copy(update={"state": IdemState.UNKNOWN, "updated_at": now})
            self.records[row.key] = new
            updated.append(new)
        return updated

    async def purge(self, now: datetime, limit: int) -> int:
        self._check()
        doomed = [
            k for k, r in self.records.items() if r.state in _PURGEABLE and r.expires_at <= now
        ][:limit]
        for key in doomed:
            del self.records[key]
        return len(doomed)

    async def unknown(self, limit: int) -> list[IdempotencyRecord]:
        self._check()
        rows = [r for r in self.records.values() if r.state is IdemState.UNKNOWN]
        return sorted(rows, key=lambda r: r.updated_at)[:limit]

    async def resolve(
        self, key: RecordKey, to_state: IdemState, now: datetime, expires_at: datetime
    ) -> IdempotencyRecord | None:
        self._check()
        row = self.records.get(key)
        if row is None or row.state is not IdemState.UNKNOWN:
            return None
        new = row.model_copy(
            update={
                "state": to_state,
                "lease_expires_at": None,
                "expires_at": expires_at,
                "updated_at": now,
            }
        )
        self.records[key] = new
        return new


class MemoryOwnerAlerts:
    def __init__(self) -> None:
        self.alerts: list[tuple[AlertKind, RecordKey, str]] = []

    def alert(self, kind: AlertKind, key: RecordKey, correlation_id: str) -> None:
        self.alerts.append((kind, key, correlation_id))


class Fault(StrEnum):
    """What the stub backend does with the next call."""

    ACK = "ack"
    ACK_THEN_FAIL = "ack_then_fail"
    NOT_SENT = "not_sent"
    REJECTED_NO_EFFECT = "rejected_no_effect"
    SENT_NO_RESPONSE = "sent_no_response"
    RAISE_AFTER_SEND = "raise_after_send"
    SUCCESS_WITHOUT_STATUS = "success_without_status"


class FaultyConnector:
    """A stub backend usable as the pipeline terminal. Every §7.4 branch on demand.

    ``calls`` counts requests that reached it (the upstream counter). ``script`` overrides
    ``mode`` for the next calls, in order. ``gate``, when set, holds every call until it opens.
    """

    def __init__(self, mode: Fault = Fault.ACK) -> None:
        self.mode = mode
        self.script: deque[Fault] = deque()
        self.gate: asyncio.Event | None = None
        self.calls = 0
        self.entered = asyncio.Event()

    async def __call__(self, ctx: CallContext, request: ToolRequest) -> ToolResult:
        del request
        self.calls += 1
        call_number = self.calls
        mode = self.script.popleft() if self.script else self.mode
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        meta = ResponseMeta(correlation_id=ctx.correlation_id)
        match mode:
            case Fault.ACK | Fault.SUCCESS_WITHOUT_STATUS:
                return ToolResult(
                    outcome=ToolSuccess(content={"effect": call_number}, meta=meta),
                    delivery=DeliveryStatus.ACKED if mode is Fault.ACK else None,
                )
            case Fault.ACK_THEN_FAIL:
                return _failed(ErrorCode.CONTRACT_VIOLATION, DeliveryStatus.ACKED, meta)
            case Fault.NOT_SENT:
                return _failed(ErrorCode.UPSTREAM_UNAVAILABLE, DeliveryStatus.NOT_SENT, meta)
            case Fault.REJECTED_NO_EFFECT:
                return _failed(ErrorCode.INVALID_INPUT, DeliveryStatus.REJECTED_NO_EFFECT, meta)
            case Fault.SENT_NO_RESPONSE:
                return _failed(
                    ErrorCode.UPSTREAM_UNAVAILABLE, DeliveryStatus.SENT_NO_RESPONSE, meta
                )
            case Fault.RAISE_AFTER_SEND:
                msg = "simulated connection reset after send"
                raise ConnectionResetError(msg)


def _failed(code: ErrorCode, delivery: DeliveryStatus, meta: ResponseMeta) -> ToolResult:
    return ToolResult(
        outcome=ToolFailure(error=AdapterError(code=code), meta=meta), delivery=delivery
    )
