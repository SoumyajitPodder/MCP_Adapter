"""Ports the observability service depends on."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from enum import StrEnum
from typing import Protocol

from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonValue
from adapter_verify.observability.domain.attributes import SpanAttributes, SpanName
from adapter_verify.observability.domain.audit import AuditEvent, AuditRecord, ChainHead
from adapter_verify.observability.domain.events import CallEvent


class SpanHandle(Protocol):
    @property
    def trace_id(self) -> str: ...

    @property
    def span_id(self) -> str: ...

    def update(self, attrs: SpanAttributes) -> None:
        """Add or overwrite attributes. Only typed attributes exist; there is no raw setter."""
        ...

    def fail(self, code: ErrorCode) -> None:
        """Mark the span as failed with a shared error code (never a message)."""
        ...


class Telemetry(Protocol):
    def span(self, name: SpanName, attrs: SpanAttributes) -> AbstractContextManager[SpanHandle]:
        """Open a child of the current span. Exceptions pass through without being recorded."""
        ...


class EventSink(Protocol):
    def emit(self, event: CallEvent) -> None:
        """Hand off an event. Must not block the request path and must not raise."""
        ...


class EventWriter(Protocol):
    async def write(self, events: Sequence[CallEvent]) -> None:
        """Durably store a batch. May raise; the buffering sink handles failure."""
        ...


class TraceQuery(Protocol):
    async def by_correlation_id(self, correlation_id: str) -> list[CallEvent]:
        """Every stored event for one correlation ID, oldest first."""
        ...


class AuditUnavailableError(Exception):
    """The audit store could not durably record an event. Carries no upstream detail."""


class AuditStore(Protocol):
    async def append(self, chain_id: str, event: AuditEvent) -> AuditRecord:
        """Link ``event`` after the chain's head, atomically. Raises AuditUnavailableError."""
        ...

    async def read_chain(self, chain_id: str) -> list[AuditRecord]:
        """All records of one chain, ascending ``seq``."""
        ...

    async def heads(self) -> list[ChainHead]:
        """The newest record of every chain."""
        ...


class PayloadKind(StrEnum):
    REQUEST = "request"
    UPSTREAM_RESPONSE = "upstream_response"
    RESULT = "result"


class PayloadStore(Protocol):
    """The only place bodies may live (§8.4): encrypted at rest, access-controlled.

    Production adapter pending the storage and secret-manager decisions (brief §15).
    TODO(owner): ``get`` gains a principal once the payload permission model exists.
    """

    async def put(self, correlation_id: str, kind: PayloadKind, body: JsonValue) -> str:
        """Store ``body`` and return an opaque reference safe to log."""
        ...

    async def get(self, ref: str) -> JsonValue:
        """Return a stored body. Raises KeyError for an unknown or expired reference."""
        ...


class Diagnostics(Protocol):
    def internal_error(self, correlation_id: str, summary: str) -> None:
        """Record details of an INTERNAL error. ``summary`` is already log-safe."""
        ...
