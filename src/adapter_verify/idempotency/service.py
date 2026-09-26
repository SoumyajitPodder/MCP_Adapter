"""Duplicate prevention (brief §7): the pipeline stage and the maintenance jobs.

Reserve, then execute, then record. The reservation is audited before anything is sent; a
state that cannot be proven becomes UNKNOWN and goes to a person, never back to "free".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from adapter_kernel.context import CallContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    Next,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_verify.common.ports import Clock, Entropy
from adapter_verify.idempotency.domain.decide import (
    MIN_RETRY_AFTER_MS,
    Action,
    ActionKind,
    Settlement,
    decide,
    settle,
    unknown_outcome,
)
from adapter_verify.idempotency.domain.keys import FingerprintError, fingerprint, is_valid_key
from adapter_verify.idempotency.domain.records import (
    AlertKind,
    IdempotencyRecord,
    IdemState,
    RecordKey,
    Reservation,
    Timing,
    Transition,
)
from adapter_verify.idempotency.ports import (
    IdempotencyStore,
    IdempotencyStoreUnavailableError,
    OwnerAlerts,
)
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.domain.attributes import Outcome, SpanAttributes, SpanName
from adapter_verify.observability.domain.audit import AuditKind, DetailValue, PrincipalKind
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.domain.exceptions import safe_exception_summary
from adapter_verify.observability.domain.ids import UUID7_RANDOM_BYTES, uuid7_from
from adapter_verify.observability.ports import (
    AuditUnavailableError,
    Diagnostics,
    EventSink,
    PayloadKind,
    PayloadStore,
    SpanHandle,
    Telemetry,
)

SWEEPER_PRINCIPAL: Final = "idempotency-sweeper"
MANUAL_RESOLUTION_WARNING: Final = "idempotency:resolved-manually"
"""Replay of a row a person resolved as completed: the effect happened, no result is stored."""

_OPEN: Final = frozenset({IdemState.RESERVED})
_OPEN_OR_EXPIRED: Final = frozenset({IdemState.RESERVED, IdemState.UNKNOWN})


@dataclass(frozen=True)
class IdempotencyAudit:
    """Every state change goes on the audit trail (§8.7)."""

    trail: AuditTrail
    diagnostics: Diagnostics

    async def transition(  # noqa: PLR0913 - keyword-only audit fields
        self,
        principal_kind: PrincipalKind,
        principal: str,
        record: RecordKey,
        correlation_id: str,
        *,
        from_state: IdemState | None,
        to_state: IdemState,
        attempt_id: str,
        error_code: ErrorCode | None = None,
        kind: AuditKind = AuditKind.IDEMPOTENCY_TRANSITION,
        extra: dict[str, DetailValue] | None = None,
    ) -> bool:
        """Audit one state change. False, with a diagnostic, when the audit store is down."""
        detail: dict[str, DetailValue] = {
            "agent_id": record.agent_id,
            "tool": record.tool,
            "key": record.key,
            "from_state": None if from_state is None else from_state.value,
            "to_state": to_state.value,
            "attempt_id": attempt_id,
            "error_code": None if error_code is None else error_code.value,
            **(extra or {}),
        }
        try:
            await self.trail.record(
                principal_kind=principal_kind,
                principal=principal,
                kind=kind,
                correlation_id=correlation_id,
                detail=detail,
            )
        except AuditUnavailableError:
            self.diagnostics.internal_error(correlation_id, "idempotency audit unavailable")
            return False
        return True


@dataclass(frozen=True)
class IdempotencyObserver:
    """Where the stage reports: spans, call events, audit and owner alerts."""

    telemetry: Telemetry
    events: EventSink
    audit: IdempotencyAudit
    alerts: OwnerAlerts
    clock: Clock

    @property
    def diagnostics(self) -> Diagnostics:
        return self.audit.diagnostics

    def emit(  # noqa: PLR0913 - keyword-only event fields
        self,
        span: SpanHandle,
        ctx: CallContext,
        *,
        stage: SpanName,
        error_code: ErrorCode | None,
        state: IdemState | None = None,
        replayed: bool | None = None,
    ) -> None:
        outcome = Outcome.OK if error_code is None else Outcome.ERROR
        key = ctx.request.idempotency_key
        span.update(
            SpanAttributes(
                outcome=outcome,
                idempotency_state=None if state is None else state.value,
                replayed=replayed,
            )
        )
        if error_code is not None:
            span.fail(error_code)
        self.events.emit(
            CallEvent(
                timestamp=self.clock.now(),
                trace_id=span.trace_id,
                span_id=span.span_id,
                correlation_id=ctx.correlation_id,
                agent_id=ctx.agent_id,
                tool=ctx.tool,
                semantic_version=ctx.semantic_version,
                stage=stage,
                behavior=ctx.behavior,
                outcome=outcome,
                error_code=error_code,
                idempotency_key=key if key is not None and is_valid_key(key) else None,
                idempotency_state=None if state is None else state.value,
                replayed=replayed,
            )
        )


@dataclass(frozen=True)
class _Held:
    """This attempt holds the key; ``from_state`` is None for a new key."""

    from_state: IdemState | None


def _failure(code: ErrorCode, ctx: CallContext, *, retry_after_ms: int | None = None) -> ToolResult:
    return ToolResult(
        outcome=ToolFailure(
            error=AdapterError(code=code, retry_after_ms=retry_after_ms),
            meta=ResponseMeta(correlation_id=ctx.correlation_id),
        )
    )


class IdempotencyStage:
    """§7 stage. Read-only tools pass through; state-changing tools need a key (§7.1)."""

    name = "idempotency.reserve"

    def __init__(
        self,
        store: IdempotencyStore,
        payloads: PayloadStore,
        observer: IdempotencyObserver,
        timing: Timing,
        entropy: Entropy,
    ) -> None:
        self._store = store
        self._payloads = payloads
        self._observer = observer
        self._timing = timing
        self._entropy = entropy

    async def __call__(self, ctx: CallContext, request: ToolRequest, call_next: Next) -> ToolResult:
        if not ctx.behavior.changes_state:
            return await call_next(ctx, request)
        with self._observer.telemetry.span(
            SpanName.IDEMPOTENCY_RESERVE,
            SpanAttributes(
                agent_id=ctx.agent_id, tool=ctx.tool, semantic_version=ctx.semantic_version
            ),
        ) as span:
            admitted = await self._admit(span, ctx, request)
        if isinstance(admitted, ToolResult):
            return admitted
        try:
            result = await call_next(ctx, request)
        except Exception:
            # Nothing proves the request did not leave (§7.4). Record UNKNOWN, then re-raise so
            # the entry turns it into INTERNAL.
            await self._finish(ctx, admitted, unknown_outcome(), None)
            raise
        return await self._finish(ctx, admitted, settle(result), result)

    # ------------------------------------------------------------------ admission

    async def _admit(
        self, span: SpanHandle, ctx: CallContext, request: ToolRequest
    ) -> ToolResult | Reservation:
        reservation = self._reservation(ctx, request)
        if isinstance(reservation, ErrorCode):
            return self._reject(span, ctx, reservation)
        try:
            outcome = await self._reserve(reservation)
        except IdempotencyStoreUnavailableError:
            self._observer.diagnostics.internal_error(
                ctx.correlation_id, "idempotency store unavailable"
            )
            return self._reject(span, ctx, ErrorCode.INTERNAL)
        if isinstance(outcome, tuple):
            record, action = outcome
            return await self._answer(span, ctx, record, action)
        audited = await self._observer.audit.transition(
            PrincipalKind.AGENT,
            ctx.agent_id,
            reservation.key,
            ctx.correlation_id,
            from_state=outcome.from_state,
            to_state=IdemState.RESERVED,
            attempt_id=str(reservation.attempt_id),
        )
        if not audited:
            # Nothing was sent: release the key rather than leave it to expire into UNKNOWN.
            await self._release(reservation)
            return self._reject(span, ctx, ErrorCode.INTERNAL)
        self._observer.emit(
            span,
            ctx,
            stage=SpanName.IDEMPOTENCY_RESERVE,
            error_code=None,
            state=IdemState.RESERVED,
        )
        return reservation

    async def _reserve(
        self, reservation: Reservation
    ) -> _Held | tuple[IdempotencyRecord | None, Action]:
        """Hold the key, or return the record that blocks it and what to answer."""
        existing = await self._store.reserve(reservation)
        if existing is None:
            return _Held(from_state=None)
        action = self._decide(existing, reservation)
        if action.kind is not ActionKind.RERESERVE:
            return existing, action
        if await self._store.rereserve(reservation):
            return _Held(from_state=IdemState.FAILED_RETRYABLE)
        # Another retry re-reserved it first, or it was purged: tell this one to wait.
        current = await self._store.get(reservation.key)
        if current is None:
            return None, _busy()
        action = self._decide(current, reservation)
        return current, _busy() if action.kind is ActionKind.RERESERVE else action

    def _decide(self, record: IdempotencyRecord, reservation: Reservation) -> Action:
        return decide(record, reservation.fingerprint, reservation.semantic_version, reservation.at)

    async def _answer(
        self,
        span: SpanHandle,
        ctx: CallContext,
        record: IdempotencyRecord | None,
        action: Action,
    ) -> ToolResult:
        match action.kind:
            case ActionKind.REPLAY if record is not None:
                return await self._replay(span, ctx, record)
            case ActionKind.CONFLICT:
                return self._reject(span, ctx, ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
            case ActionKind.RECONCILIATION_PENDING:
                return self._reject(span, ctx, ErrorCode.RECONCILIATION_PENDING)
            case _:
                result = _failure(
                    ErrorCode.DUPLICATE_IN_PROGRESS,
                    ctx,
                    retry_after_ms=action.retry_after_ms or _busy().retry_after_ms,
                )
                self._emit_failure(span, ctx, ErrorCode.DUPLICATE_IN_PROGRESS)
                return result

    async def _replay(
        self, span: SpanHandle, ctx: CallContext, record: IdempotencyRecord
    ) -> ToolResult:
        meta = ResponseMeta(correlation_id=ctx.correlation_id, replayed=True)
        if record.error_code is not None:
            outcome: ToolSuccess | ToolFailure = ToolFailure(
                error=AdapterError(code=record.error_code), meta=meta
            )
        elif record.result_ref is None:
            outcome = ToolSuccess(
                content={}, meta=meta.model_copy(update={"warnings": (MANUAL_RESOLUTION_WARNING,)})
            )
        else:
            try:
                content = await self._payloads.get(record.result_ref)
            except Exception as exc:  # noqa: BLE001 - any payload-store failure: never re-execute
                self._observer.diagnostics.internal_error(
                    ctx.correlation_id, safe_exception_summary(exc)
                )
                self._observer.alerts.alert(
                    AlertKind.REPLAY_UNAVAILABLE, record.key, ctx.correlation_id
                )
                return self._reject(span, ctx, ErrorCode.INTERNAL)
            if not isinstance(content, dict):
                self._observer.alerts.alert(
                    AlertKind.REPLAY_UNAVAILABLE, record.key, ctx.correlation_id
                )
                return self._reject(span, ctx, ErrorCode.INTERNAL)
            outcome = ToolSuccess(content=content, meta=meta)
        code = outcome.error.code if isinstance(outcome, ToolFailure) else None
        self._observer.emit(
            span, ctx, stage=SpanName.IDEMPOTENCY_RESERVE, error_code=code, replayed=True
        )
        return ToolResult(outcome=outcome)

    def _reject(self, span: SpanHandle, ctx: CallContext, code: ErrorCode) -> ToolResult:
        self._emit_failure(span, ctx, code)
        return _failure(code, ctx)

    def _emit_failure(self, span: SpanHandle, ctx: CallContext, code: ErrorCode) -> None:
        self._observer.emit(span, ctx, stage=SpanName.IDEMPOTENCY_RESERVE, error_code=code)

    def _reservation(self, ctx: CallContext, request: ToolRequest) -> Reservation | ErrorCode:
        """This attempt's reservation, or why the call is rejected before touching the store."""
        key = ctx.request.idempotency_key
        if key is None:
            return ErrorCode.IDEMPOTENCY_KEY_REQUIRED
        if not is_valid_key(key):
            return ErrorCode.INVALID_INPUT
        try:
            fp = fingerprint(request.arguments)
        except FingerprintError:
            return ErrorCode.INVALID_INPUT
        now = self._observer.clock.now()
        attempt = uuid7_from(
            int(now.timestamp() * 1000), self._entropy.token_bytes(UUID7_RANDOM_BYTES)
        )
        return Reservation(
            key=RecordKey(agent_id=ctx.agent_id, tool=ctx.tool, key=key),
            fingerprint=fp,
            semantic_version=ctx.semantic_version,
            attempt_id=attempt,
            correlation_id=ctx.correlation_id,
            at=now,
            lease_expires_at=now + self._timing.lease_for(ctx.tool),
            expires_at=now + self._timing.retention_for(ctx.tool),
        )

    async def _release(self, reservation: Reservation) -> None:
        try:
            await self._store.transition(
                Transition(
                    key=reservation.key,
                    attempt_id=reservation.attempt_id,
                    from_states=_OPEN,
                    to_state=IdemState.FAILED_RETRYABLE,
                    at=self._observer.clock.now(),
                )
            )
        except IdempotencyStoreUnavailableError:
            # The lease expires into UNKNOWN: safe, if noisier than a release.
            self._observer.diagnostics.internal_error(
                reservation.correlation_id, "idempotency release failed"
            )

    # ------------------------------------------------------------------ settlement

    async def _finish(
        self,
        ctx: CallContext,
        attempt: Reservation,
        settlement: Settlement,
        result: ToolResult | None,
    ) -> ToolResult:
        """Write the final state, fenced by this attempt's ID, and answer the agent."""
        observer = self._observer
        with observer.telemetry.span(
            SpanName.IDEMPOTENCY_SETTLE,
            SpanAttributes(
                agent_id=ctx.agent_id, tool=ctx.tool, semantic_version=ctx.semantic_version
            ),
        ) as span:
            ref = None
            stored = result.outcome if result is not None else None
            if settlement.store_result and isinstance(stored, ToolSuccess):
                ref = await self._store_result(ctx, stored)
                if ref is None:
                    settlement = unknown_outcome()
            late_ok = (
                result is not None
                and result.delivery is DeliveryStatus.ACKED
                and settlement.state is IdemState.COMPLETED
            )
            transition = Transition(
                key=attempt.key,
                attempt_id=attempt.attempt_id,
                from_states=_OPEN_OR_EXPIRED if late_ok else _OPEN,
                to_state=settlement.state,
                result_ref=ref,
                error_code=settlement.stored_error,
                at=observer.clock.now(),
            )
            try:
                previous = await self._store.transition(transition)
            except IdempotencyStoreUnavailableError:
                observer.diagnostics.internal_error(ctx.correlation_id, "idempotency settle failed")
                observer.alerts.alert(AlertKind.SETTLEMENT_FAILED, attempt.key, ctx.correlation_id)
                previous, fenced_out = None, False
            else:
                fenced_out = previous is None
            if fenced_out:
                # The lease expired and the sweeper (or a person) took the row: its outcome is
                # no longer this attempt's to decide.
                observer.diagnostics.internal_error(
                    ctx.correlation_id, "idempotency settlement fenced out"
                )
                settlement = unknown_outcome().model_copy(update={"alert": None})
            elif previous is not None:
                await observer.audit.transition(
                    PrincipalKind.AGENT,
                    ctx.agent_id,
                    attempt.key,
                    ctx.correlation_id,
                    from_state=previous,
                    to_state=settlement.state,
                    attempt_id=str(attempt.attempt_id),
                    error_code=settlement.stored_error,
                )  # after an effect an audit failure cannot undo it: the diagnostic is the alert
            if settlement.alert is not None:
                observer.alerts.alert(settlement.alert, attempt.key, ctx.correlation_id)
            answer = (
                result
                if result is not None and settlement.agent_error is None
                else _failure(settlement.agent_error or ErrorCode.INTERNAL, ctx)
            )
            code = answer.outcome.error.code if isinstance(answer.outcome, ToolFailure) else None
            observer.emit(
                span,
                ctx,
                stage=SpanName.IDEMPOTENCY_SETTLE,
                error_code=code,
                state=None if previous is None else settlement.state,
            )
        return answer

    async def _store_result(self, ctx: CallContext, outcome: ToolSuccess) -> str | None:
        try:
            return await self._payloads.put(ctx.correlation_id, PayloadKind.RESULT, outcome.content)
        except Exception as exc:  # noqa: BLE001 - the effect happened; escalate, never re-execute
            self._observer.diagnostics.internal_error(
                ctx.correlation_id, safe_exception_summary(exc)
            )
            return None


def _busy() -> Action:
    return Action(kind=ActionKind.IN_PROGRESS, retry_after_ms=MIN_RETRY_AFTER_MS)


# ---------------------------------------------------------------------- maintenance


class Resolution(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def state(self) -> IdemState:
        return IdemState.COMPLETED if self is Resolution.COMPLETED else IdemState.FAILED_RETRYABLE


@dataclass(frozen=True)
class Operator:
    """Who runs ``idem resolve``. Until operator auth exists: a flag plus the OS user (M3-Q3)."""

    name: str
    os_user: str


class IdempotencyMaintenance:
    """Sweeper, retention and manual reconciliation (§7.4, §7.7, §7.8)."""

    def __init__(
        self,
        store: IdempotencyStore,
        audit: IdempotencyAudit,
        alerts: OwnerAlerts,
        clock: Clock,
        timing: Timing,
    ) -> None:
        self._store = store
        self._audit = audit
        self._alerts = alerts
        self._clock = clock
        self._timing = timing

    async def sweep(self, limit: int = 1_000) -> list[IdempotencyRecord]:
        """Expired RESERVED rows become UNKNOWN, each audited and alerted. Raises
        AuditUnavailableError after the batch if any audit write failed."""
        expired = await self._store.expire_leases(self._clock.now(), limit)
        failed = 0
        for record in expired:
            audited = await self._audit.transition(
                PrincipalKind.SYSTEM,
                SWEEPER_PRINCIPAL,
                record.key,
                record.correlation_id,
                from_state=IdemState.RESERVED,
                to_state=IdemState.UNKNOWN,
                attempt_id=str(record.attempt_id),
            )
            failed += not audited
            self._alerts.alert(AlertKind.LEASE_EXPIRED, record.key, record.correlation_id)
        if failed:
            raise AuditUnavailableError
        return expired

    async def purge(self, limit: int = 10_000) -> int:
        return await self._store.purge(self._clock.now(), limit)

    async def unknown(self, limit: int = 1_000) -> Sequence[IdempotencyRecord]:
        return await self._store.unknown(limit)

    async def resolve(
        self, key: RecordKey, resolution: Resolution, operator: Operator, reason: str
    ) -> IdempotencyRecord | None:
        """Settle an UNKNOWN row by hand. Retention restarts so a resolved key outlives the
        agent's retries. The audit record goes on the operator's chain (§8.7)."""
        if not reason.strip():
            msg = "a reason is required"
            raise ValueError(msg)
        if not is_valid_key(operator.name):
            msg = "operator must match [A-Za-z0-9._:-]{1,128}"
            raise ValueError(msg)
        now = self._clock.now()
        record = await self._store.resolve(
            key, resolution.state, now, now + self._timing.retention_for(key.tool)
        )
        if record is None:
            return None
        audited = await self._audit.transition(
            PrincipalKind.OPERATOR,
            operator.name,
            key,
            record.correlation_id,
            from_state=IdemState.UNKNOWN,
            to_state=record.state,
            attempt_id=str(record.attempt_id),
            kind=AuditKind.MANUAL_RECONCILIATION,
            extra={"reason": reason, "os_user": operator.os_user},
        )
        if not audited:
            raise AuditUnavailableError
        return record
