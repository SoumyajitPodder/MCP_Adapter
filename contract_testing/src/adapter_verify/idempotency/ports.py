"""Ports the idempotency service depends on."""

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from adapter_verify.idempotency.domain.records import (
    AlertKind,
    IdempotencyRecord,
    IdemState,
    RecordKey,
    Reservation,
    Transition,
)


class IdempotencyStoreUnavailableError(Exception):
    """The store could not be read or written. Carries no driver detail."""


class IdempotencyStore(Protocol):
    async def reserve(self, reservation: Reservation) -> IdempotencyRecord | None:
        """Insert a RESERVED row in one atomic statement (§7.6). None when inserted; otherwise
        the record that already holds the key."""
        ...

    async def rereserve(self, reservation: Reservation) -> bool:
        """Re-reserve a FAILED_RETRYABLE row with the same fingerprint and version, rotating
        ``attempt_id``. False when another attempt got there first."""
        ...

    async def get(self, key: RecordKey) -> IdempotencyRecord | None: ...

    async def transition(self, transition: Transition) -> IdemState | None:
        """Apply a fenced state change and return the state it left. None when the fence
        (attempt, state) no longer holds."""
        ...

    async def expire_leases(self, now: datetime, limit: int) -> list[IdempotencyRecord]:
        """Move RESERVED rows whose lease expired to UNKNOWN; return them as updated."""
        ...

    async def purge(self, now: datetime, limit: int) -> int:
        """Delete expired COMPLETED and FAILED_RETRYABLE rows. Never deletes UNKNOWN rows."""
        ...

    async def unknown(self, limit: int) -> list[IdempotencyRecord]:
        """Rows awaiting reconciliation, oldest first."""
        ...

    async def resolve(
        self, key: RecordKey, to_state: IdemState, now: datetime, expires_at: datetime
    ) -> IdempotencyRecord | None:
        """Manually settle an UNKNOWN row. None when the row is missing or not UNKNOWN."""
        ...


class OwnerAlerts(Protocol):
    def alert(self, kind: AlertKind, key: RecordKey, correlation_id: str) -> None:
        """Tell the tool owner (§7.8). Must not block the request path and must not raise."""
        ...


class ReadBack(StrEnum):
    EFFECT_FOUND = "effect_found"
    NO_EFFECT = "no_effect"
    UNDETERMINED = "undetermined"


class Reconciler(Protocol):
    """Automatic read-back for one backend (§7.8), e.g. find an order by external reference.

    No Phase 1 adapter: UNKNOWN rows are resolved by a person (``idem resolve``).
    """

    async def read_back(self, record: IdempotencyRecord) -> ReadBack: ...
