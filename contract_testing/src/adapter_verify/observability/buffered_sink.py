"""Bounded, non-blocking event buffer in front of a durable writer (brief §8.8).

``emit`` never blocks and never raises. When the buffer is full, sampled (droppable) events
are evicted first; must-keep events are dropped only when nothing droppable remains, and each
such episode raises one diagnostic alert.
"""

import asyncio
from collections import deque

from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.domain.exceptions import safe_exception_summary
from adapter_verify.observability.domain.sampling import must_keep
from adapter_verify.observability.ports import Diagnostics, EventWriter

DIAGNOSTIC_ID = "event-sink"


class BufferedEventSink:
    def __init__(
        self,
        writer: EventWriter,
        diagnostics: Diagnostics,
        *,
        capacity: int,
        batch_size: int,
    ) -> None:
        if capacity < 1 or batch_size < 1:
            msg = "capacity and batch_size must be positive"
            raise ValueError(msg)
        self._writer = writer
        self._diagnostics = diagnostics
        self._capacity = capacity
        self._batch_size = batch_size
        self._critical: deque[CallEvent] = deque()
        self._sampled: deque[CallEvent] = deque()
        self._wakeup = asyncio.Event()
        self._alerted = False
        self.dropped_sampled = 0
        self.dropped_critical = 0

    @property
    def pending(self) -> int:
        return len(self._critical) + len(self._sampled)

    def emit(self, event: CallEvent) -> None:
        keep = must_keep(event)
        if self.pending >= self._capacity:
            if self._sampled:
                self._sampled.popleft()
                self.dropped_sampled += 1
            elif not keep:
                self.dropped_sampled += 1
                return
            else:
                self._drop_critical()
                return
        (self._critical if keep else self._sampled).append(event)
        self._wakeup.set()

    async def flush(self) -> None:
        """Write everything buffered. On a write failure, keep must-keep events and stop."""
        while self.pending:
            batch = self._take_batch()
            try:
                await self._writer.write(batch)
            except Exception as exc:  # noqa: BLE001 - any writer failure is handled the same way
                self._diagnostics.internal_error(DIAGNOSTIC_ID, safe_exception_summary(exc))
                self._requeue(batch)
                return
            self._alerted = False

    async def run(self, interval_s: float) -> None:
        """Flush whenever events arrive, at most every ``interval_s``. Cancel to stop."""
        try:
            while True:
                await self._wakeup.wait()
                self._wakeup.clear()
                await self.flush()
                await asyncio.sleep(interval_s)
        finally:
            await self.flush()

    def _take_batch(self) -> list[CallEvent]:
        batch: list[CallEvent] = []
        for source in (self._critical, self._sampled):
            while source and len(batch) < self._batch_size:
                batch.append(source.popleft())
        return batch

    def _requeue(self, batch: list[CallEvent]) -> None:
        for event in reversed(batch):
            if not must_keep(event):
                self.dropped_sampled += 1
            elif self.pending < self._capacity:
                self._critical.appendleft(event)
            else:
                self._drop_critical()

    def _drop_critical(self) -> None:
        self.dropped_critical += 1
        if not self._alerted:
            self._alerted = True
            self._diagnostics.internal_error(DIAGNOSTIC_ID, "event buffer full: must-keep dropped")
