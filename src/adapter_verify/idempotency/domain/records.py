"""Idempotency records, their states (brief §7.4) and per-tool timing (M3-Q2)."""

from datetime import timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from adapter_kernel.errors import ErrorCode
from adapter_verify.common.model import FrozenModel
from adapter_verify.idempotency.domain.keys import FINGERPRINT_BYTES, KEY_PATTERN


class IdemState(StrEnum):
    RESERVED = "RESERVED"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    UNKNOWN = "UNKNOWN"


class AlertKind(StrEnum):
    """Owner alerts (§7.8). Each names a case a person may have to act on."""

    OUTCOME_UNKNOWN = "outcome_unknown"
    """The request may have reached the backend and no answer came back."""
    LEASE_EXPIRED = "lease_expired"
    """An attempt outlived its lease (crash or hang); the sweeper marked it UNKNOWN."""
    EFFECT_WITH_ERROR = "effect_with_error"
    """The backend acknowledged, then a later stage failed: an effect the agent cannot see."""
    SUCCESS_WITHOUT_ACK = "success_without_ack"
    """A success arrived without an ACKED delivery status: a connector reporting bug."""
    SETTLEMENT_FAILED = "settlement_failed"
    """The final state could not be written; the lease will expire into UNKNOWN."""
    REPLAY_UNAVAILABLE = "replay_unavailable"
    """A completed result could not be read back from the payload store."""


class RecordKey(FrozenModel):
    """Keys are scoped per agent and tool (§7.2): one agent's key never collides with another's."""

    agent_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    key: str = Field(pattern=KEY_PATTERN)


class Reservation(FrozenModel):
    """A new attempt: inserted as RESERVED, or re-reserving a FAILED_RETRYABLE row."""

    key: RecordKey
    fingerprint: bytes = Field(min_length=FINGERPRINT_BYTES, max_length=FINGERPRINT_BYTES)
    semantic_version: str = Field(min_length=1)
    attempt_id: UUID = Field(description="Fencing token (R-005); rotated on every re-reserve.")
    correlation_id: str = Field(min_length=1)
    at: AwareDatetime
    lease_expires_at: AwareDatetime
    expires_at: AwareDatetime


class IdempotencyRecord(FrozenModel):
    """One idempotency record: brief §7.7 plus the fencing token and the tool version."""

    key: RecordKey = Field(description="Agent, tool and caller key.")
    fingerprint: bytes = Field(
        min_length=FINGERPRINT_BYTES,
        max_length=FINGERPRINT_BYTES,
        description="SHA-256 of the RFC 8785 arguments.",
    )
    state: IdemState = Field(description="RESERVED, COMPLETED, FAILED_RETRYABLE or UNKNOWN.")
    semantic_version: str = Field(min_length=1, description="Tool version of the attempt.")
    attempt_id: UUID = Field(description="Fencing token of the attempt that holds the row.")
    correlation_id: str = Field(min_length=1, description="Correlation ID of that attempt.")
    result_ref: str | None = Field(description="Payload-store reference; bodies never live here.")
    error_code: ErrorCode | None = Field(description="Post-acknowledgement failure (R-004).")
    lease_expires_at: AwareDatetime | None = Field(description="Set while RESERVED.")
    expires_at: AwareDatetime = Field(description="End of retention; purge never deletes UNKNOWN.")
    created_at: AwareDatetime = Field(description="First reservation.")
    updated_at: AwareDatetime = Field(description="Last state change.")

    @classmethod
    def reserved(cls, r: Reservation) -> Self:
        return cls(
            key=r.key,
            fingerprint=r.fingerprint,
            state=IdemState.RESERVED,
            semantic_version=r.semantic_version,
            attempt_id=r.attempt_id,
            correlation_id=r.correlation_id,
            result_ref=None,
            error_code=None,
            lease_expires_at=r.lease_expires_at,
            expires_at=r.expires_at,
            created_at=r.at,
            updated_at=r.at,
        )


class Transition(FrozenModel):
    """A fenced state change: applied only if the row still holds ``attempt_id`` and one of
    ``from_states``."""

    key: RecordKey
    attempt_id: UUID
    from_states: frozenset[IdemState] = Field(min_length=1)
    to_state: IdemState
    result_ref: str | None = None
    error_code: ErrorCode | None = None
    at: AwareDatetime


class Timing(FrozenModel):
    """Lease and retention, with per-tool overrides from adapter config (M3-Q2)."""

    lease_s: int = Field(ge=1, le=3_600)
    retention_h: int = Field(ge=1, le=24 * 365)
    lease_overrides_s: dict[str, int] = Field(default_factory=dict)
    retention_overrides_h: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sane(self) -> Self:
        leases = [self.lease_s, *self.lease_overrides_s.values()]
        retentions = [self.retention_h, *self.retention_overrides_h.values()]
        if min(leases) < 1 or min(retentions) < 1:
            msg = "leases and retentions must be positive"
            raise ValueError(msg)
        if max(leases) > min(retentions) * 3_600:
            msg = "retention must be longer than every lease"
            raise ValueError(msg)
        return self

    def lease_for(self, tool: str) -> timedelta:
        return timedelta(seconds=self.lease_overrides_s.get(tool, self.lease_s))

    def retention_for(self, tool: str) -> timedelta:
        return timedelta(hours=self.retention_overrides_h.get(tool, self.retention_h))
