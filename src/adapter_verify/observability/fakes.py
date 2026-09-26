"""In-memory implementations of the observability ports (DESIGN.md X-10).

Used by unit tests, the golden sandbox and the sentinel scan: every sink keeps everything it
was given so tests can assert on it.
"""

import copy
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import count

from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonValue
from adapter_verify.observability.domain.attributes import (
    AttributeValue,
    SpanAttributes,
    SpanName,
    otel_attributes,
)
from adapter_verify.observability.domain.audit import AuditEvent, AuditRecord, ChainHead, link
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.ports import AuditUnavailableError, PayloadKind


@dataclass
class RecordedSpan:
    name: SpanName
    trace_id: str
    span_id: str
    parent_span_id: str | None
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    error_code: ErrorCode | None = None
    ended: bool = False

    def update(self, attrs: SpanAttributes) -> None:
        self.attributes.update(otel_attributes(self.name, attrs))

    def fail(self, code: ErrorCode) -> None:
        self.error_code = code


class RecordingTelemetry:
    """Keeps every span, with parentage, in start order."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self._stack: list[RecordedSpan] = []
        self._ids = count(1)

    @contextmanager
    def span(self, name: SpanName, attrs: SpanAttributes) -> Iterator[RecordedSpan]:
        parent = self._stack[-1] if self._stack else None
        trace_id = parent.trace_id if parent else f"{next(self._ids):032x}"
        recorded = RecordedSpan(
            name=name,
            trace_id=trace_id,
            span_id=f"{next(self._ids):016x}",
            parent_span_id=parent.span_id if parent else None,
        )
        recorded.update(attrs)
        self.spans.append(recorded)
        self._stack.append(recorded)
        try:
            yield recorded
        finally:
            self._stack.pop()
            recorded.ended = True


class MemoryEventSink:
    def __init__(self) -> None:
        self.events: list[CallEvent] = []

    def emit(self, event: CallEvent) -> None:
        self.events.append(event)


class MemoryEventStore:
    """EventWriter + TraceQuery in memory. Set ``fail_next`` to simulate an outage."""

    def __init__(self) -> None:
        self.events: list[CallEvent] = []
        self.fail_next = 0

    async def write(self, events: Sequence[CallEvent]) -> None:
        if self.fail_next:
            self.fail_next -= 1
            msg = "simulated event store outage"
            raise ConnectionError(msg)
        self.events.extend(events)

    async def by_correlation_id(self, correlation_id: str) -> list[CallEvent]:
        found = [e for e in self.events if e.correlation_id == correlation_id]
        return sorted(found, key=lambda e: e.timestamp)


class MemoryAuditStore:
    """AuditStore in memory. ``records`` is exposed so tests can tamper with it."""

    def __init__(self) -> None:
        self.records: dict[str, list[AuditRecord]] = {}
        self.unavailable = False

    async def append(self, chain_id: str, event: AuditEvent) -> AuditRecord:
        if self.unavailable:
            raise AuditUnavailableError
        chain = self.records.setdefault(chain_id, [])
        head = chain[-1] if chain else None
        record = link(
            chain_id,
            None
            if head is None
            else ChainHead(chain_id=chain_id, seq=head.seq, record_hash=head.record_hash),
            event,
        )
        chain.append(record)
        return record

    async def read_chain(self, chain_id: str) -> list[AuditRecord]:
        return list(self.records.get(chain_id, []))

    async def heads(self) -> list[ChainHead]:
        return [
            ChainHead(chain_id=cid, seq=rs[-1].seq, record_hash=rs[-1].record_hash)
            for cid, rs in sorted(self.records.items())
            if rs
        ]


class MemoryPayloadStore:
    """PayloadStore in memory. Holds real bodies by design, so sentinel scans exempt it."""

    def __init__(self) -> None:
        self._bodies: dict[str, tuple[str, PayloadKind, JsonValue]] = {}
        self._ids = count(1)

    async def put(self, correlation_id: str, kind: PayloadKind, body: JsonValue) -> str:
        ref = f"payload:{next(self._ids):012d}"
        self._bodies[ref] = (correlation_id, kind, copy.deepcopy(body))
        return ref

    async def get(self, ref: str) -> JsonValue:
        return copy.deepcopy(self._bodies[ref][2])


class MemoryDiagnostics:
    def __init__(self) -> None:
        self.internal_errors: list[tuple[str, str]] = []

    def internal_error(self, correlation_id: str, summary: str) -> None:
        self.internal_errors.append((correlation_id, summary))
