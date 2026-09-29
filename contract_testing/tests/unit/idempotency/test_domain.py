from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import DeliveryStatus, ToolFailure, ToolResult, ToolSuccess
from adapter_verify.idempotency.domain.decide import (
    MIN_RETRY_AFTER_MS,
    ActionKind,
    decide,
    settle,
)
from adapter_verify.idempotency.domain.keys import FingerprintError, fingerprint, is_valid_key
from adapter_verify.idempotency.domain.records import (
    AlertKind,
    IdempotencyRecord,
    IdemState,
    RecordKey,
    Reservation,
    Timing,
)
from tests.unit.idempotency.support import AT

pytestmark = pytest.mark.unit

FP = b"\x01" * 32


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("order-88213:cancel", True),
        ("a", True),
        ("A.b_c:d-9", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("", False),
        ("has space", False),
        ("tab\t", False),
        ("naïve", False),
        ("line\n", False),
    ],
)
def test_key_rules(key: str, valid: bool) -> None:
    assert is_valid_key(key) is valid


def test_fingerprint_ignores_key_order_and_detects_value_changes() -> None:
    a = fingerprint({"b": [1, {"y": "2", "x": None}], "a": True})
    b = fingerprint({"a": True, "b": [1, {"x": None, "y": "2"}]})
    assert a == b
    assert len(a) == 32
    assert fingerprint({"a": True, "b": [1, {"x": None, "y": "3"}]}) != a
    assert fingerprint({"a": 1.0}) == fingerprint({"a": 1})  # one JSON number


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2**60])
def test_fingerprint_rejects_values_without_a_canonical_form(bad: float) -> None:
    with pytest.raises(FingerprintError):
        fingerprint({"x": bad})


def _record(state: IdemState, *, lease_s: float | None = 30, **kw: object) -> IdempotencyRecord:
    reservation = Reservation(
        key=RecordKey(agent_id="writer", tool="order.cancel", key="k"),
        fingerprint=FP,
        semantic_version="1.0.0",
        attempt_id=uuid4(),
        correlation_id="c",
        at=AT,
        lease_expires_at=AT + timedelta(seconds=30),
        expires_at=AT + timedelta(hours=72),
    )
    lease = None if lease_s is None else AT + timedelta(seconds=lease_s)
    return IdempotencyRecord.reserved(reservation).model_copy(
        update={"state": state, "lease_expires_at": lease, **kw}
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (IdemState.COMPLETED, ActionKind.REPLAY),
        (IdemState.RESERVED, ActionKind.IN_PROGRESS),
        (IdemState.UNKNOWN, ActionKind.RECONCILIATION_PENDING),
        (IdemState.FAILED_RETRYABLE, ActionKind.RERESERVE),
    ],
)
def test_decision_table_same_fingerprint(state: IdemState, expected: ActionKind) -> None:
    assert decide(_record(state), FP, "1.0.0", AT).kind is expected


@pytest.mark.parametrize("state", list(IdemState))
def test_different_arguments_or_version_always_conflict(state: IdemState) -> None:
    assert decide(_record(state), b"\x02" * 32, "1.0.0", AT).kind is ActionKind.CONFLICT
    assert decide(_record(state), FP, "1.1.0", AT).kind is ActionKind.CONFLICT


def test_in_progress_retry_after_is_the_remaining_lease_with_a_floor() -> None:
    record = _record(IdemState.RESERVED, lease_s=30)
    assert decide(record, FP, "1.0.0", AT + timedelta(seconds=10)).retry_after_ms == 20_000
    almost = decide(record, FP, "1.0.0", AT + timedelta(seconds=29.99))
    assert almost.retry_after_ms == MIN_RETRY_AFTER_MS


@pytest.mark.parametrize("lease_s", [None, 0, -5])
def test_expired_lease_is_never_freed(lease_s: float | None) -> None:
    record = _record(IdemState.RESERVED, lease_s=lease_s)
    assert decide(record, FP, "1.0.0", AT).kind is ActionKind.RECONCILIATION_PENDING


def _result(code: ErrorCode | None, delivery: DeliveryStatus | None) -> ToolResult:
    meta = ResponseMeta(correlation_id="c")
    outcome = (
        ToolSuccess(content={"ok": True}, meta=meta)
        if code is None
        else ToolFailure(error=AdapterError(code=code), meta=meta)
    )
    return ToolResult(outcome=outcome, delivery=delivery)


@pytest.mark.parametrize(
    ("code", "delivery", "state", "agent_error", "alert"),
    [
        (None, DeliveryStatus.ACKED, IdemState.COMPLETED, None, None),
        (None, None, IdemState.COMPLETED, None, AlertKind.SUCCESS_WITHOUT_ACK),
        (None, DeliveryStatus.NOT_SENT, IdemState.COMPLETED, None, AlertKind.SUCCESS_WITHOUT_ACK),
        (ErrorCode.DRIFT_BLOCKED, None, IdemState.FAILED_RETRYABLE, None, None),
        (
            ErrorCode.UPSTREAM_UNAVAILABLE,
            DeliveryStatus.NOT_SENT,
            IdemState.FAILED_RETRYABLE,
            None,
            None,
        ),
        (
            ErrorCode.INVALID_INPUT,
            DeliveryStatus.REJECTED_NO_EFFECT,
            IdemState.FAILED_RETRYABLE,
            None,
            None,
        ),
        (
            ErrorCode.UPSTREAM_UNAVAILABLE,
            DeliveryStatus.SENT_NO_RESPONSE,
            IdemState.UNKNOWN,
            ErrorCode.RECONCILIATION_PENDING,
            AlertKind.OUTCOME_UNKNOWN,
        ),
        (
            ErrorCode.CONTRACT_VIOLATION,
            DeliveryStatus.ACKED,
            IdemState.COMPLETED,
            ErrorCode.CONTRACT_VIOLATION,
            AlertKind.EFFECT_WITH_ERROR,
        ),
        (
            ErrorCode.UPSTREAM_UNAVAILABLE,
            DeliveryStatus.ACKED,
            IdemState.COMPLETED,
            ErrorCode.INTERNAL,
            AlertKind.EFFECT_WITH_ERROR,
        ),
    ],
)
def test_settlement_table(
    code: ErrorCode | None,
    delivery: DeliveryStatus | None,
    state: IdemState,
    agent_error: ErrorCode | None,
    alert: AlertKind | None,
) -> None:
    s = settle(_result(code, delivery))
    assert (s.state, s.agent_error, s.alert) == (state, agent_error, alert)
    assert s.store_result is (code is None)
    assert s.stored_error == (agent_error if state is IdemState.COMPLETED else None)


def test_timing_overrides_and_validation() -> None:
    timing = Timing(
        lease_s=30,
        retention_h=72,
        lease_overrides_s={"slow.tool": 120},
        retention_overrides_h={"slow.tool": 168},
    )
    assert timing.lease_for("slow.tool") == timedelta(seconds=120)
    assert timing.lease_for("other") == timedelta(seconds=30)
    assert timing.retention_for("slow.tool") == timedelta(hours=168)
    assert timing.retention_for("other") == timedelta(hours=72)
    with pytest.raises(ValidationError, match="positive"):
        Timing(lease_s=30, retention_h=72, lease_overrides_s={"t": 0})
    with pytest.raises(ValidationError, match="longer than every lease"):
        Timing(lease_s=3_600, retention_h=1, lease_overrides_s={"t": 3_601})
