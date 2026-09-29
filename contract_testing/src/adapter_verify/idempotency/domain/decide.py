"""The §7.5 decision table for a key that already has a record, and outcome settlement (R-002,
R-003, R-004). Both functions are pure."""

import math
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import Field

from adapter_kernel.errors import ERROR_SPECS, ErrorCode, RetryPolicy
from adapter_kernel.pipeline import DeliveryStatus, ToolFailure, ToolResult
from adapter_verify.common.model import FrozenModel
from adapter_verify.idempotency.domain.records import AlertKind, IdempotencyRecord, IdemState

MIN_RETRY_AFTER_MS: Final = 100


class ActionKind(StrEnum):
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    RECONCILIATION_PENDING = "reconciliation_pending"
    CONFLICT = "conflict"
    RERESERVE = "rereserve"


class Action(FrozenModel):
    kind: ActionKind
    retry_after_ms: int | None = Field(default=None, description="Set for IN_PROGRESS only.")


def decide(
    existing: IdempotencyRecord, fingerprint: bytes, semantic_version: str, now: datetime
) -> Action:
    """What to do with a call whose key already has a record. Never re-executes an effect.

    A different tool version counts as different arguments: a stored result is only ever
    replayed in the shape it was produced in.
    """
    if existing.fingerprint != fingerprint or existing.semantic_version != semantic_version:
        return Action(kind=ActionKind.CONFLICT)
    match existing.state:
        case IdemState.COMPLETED:
            return Action(kind=ActionKind.REPLAY)
        case IdemState.FAILED_RETRYABLE:
            return Action(kind=ActionKind.RERESERVE)
        case IdemState.UNKNOWN:
            return Action(kind=ActionKind.RECONCILIATION_PENDING)
        case IdemState.RESERVED:
            lease = existing.lease_expires_at
            if lease is None or lease <= now:
                # Expired lease: the sweeper will mark it UNKNOWN; never free it (§7.4).
                return Action(kind=ActionKind.RECONCILIATION_PENDING)
            remaining_ms = math.ceil((lease - now).total_seconds() * 1000)
            return Action(
                kind=ActionKind.IN_PROGRESS,
                retry_after_ms=max(MIN_RETRY_AFTER_MS, remaining_ms),
            )


class Settlement(FrozenModel):
    """How one attempt ends."""

    state: IdemState = Field(description="COMPLETED, FAILED_RETRYABLE or UNKNOWN.")
    stored_error: ErrorCode | None = Field(description="error_code written to the record.")
    agent_error: ErrorCode | None = Field(
        description="If set, the agent gets this error instead of the result as returned."
    )
    store_result: bool = Field(description="Store the success content for replay.")
    alert: AlertKind | None = Field(description="Owner alert to raise, if any.")


def settle(result: ToolResult) -> Settlement:
    """Decide the final state from what the connector knows about delivery (R-002).

    A success is proof the backend answered, so it always completes. A failure is retryable
    only when the request provably had no effect; once acknowledged it completes with the
    error recorded (R-004); when the outcome is unknown the agent is told to stop (R-003).
    """
    delivery, outcome = result.delivery, result.outcome
    if not isinstance(outcome, ToolFailure):
        alert = None if delivery is DeliveryStatus.ACKED else AlertKind.SUCCESS_WITHOUT_ACK
        return Settlement(
            state=IdemState.COMPLETED,
            stored_error=None,
            agent_error=None,
            store_result=True,
            alert=alert,
        )
    if delivery is None or not delivery.upstream_effect_possible:
        return Settlement(
            state=IdemState.FAILED_RETRYABLE,
            stored_error=None,
            agent_error=None,
            store_result=False,
            alert=None,
        )
    if delivery is DeliveryStatus.SENT_NO_RESPONSE:
        return unknown_outcome()
    # ACKED, then a later stage failed. Replays return the same error; an error that invites a
    # retry would mislead (the replay never changes), so it is recorded as INTERNAL.
    code = outcome.error.code
    stored = code if ERROR_SPECS[code].retry is RetryPolicy.NEVER else ErrorCode.INTERNAL
    return Settlement(
        state=IdemState.COMPLETED,
        stored_error=stored,
        agent_error=stored,
        store_result=False,
        alert=AlertKind.EFFECT_WITH_ERROR,
    )


def unknown_outcome() -> Settlement:
    """The request may have had an effect and nobody can say which. A person resolves it."""
    return Settlement(
        state=IdemState.UNKNOWN,
        stored_error=None,
        agent_error=ErrorCode.RECONCILIATION_PENDING,
        store_result=False,
        alert=AlertKind.OUTCOME_UNKNOWN,
    )
