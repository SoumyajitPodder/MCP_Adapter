"""Brief §8.10 acceptance, end to end on real Postgres and the real OTel SDK.

Stages §9/§4/§7/§3/§2 do not exist yet, so ``FakePipeline`` stands in for them. It uses the
same ports a real stage will (telemetry, event sink, payload store) and puts customer data
(sentinels) exactly where a real backend call would: in the upstream response body.
"""

from datetime import UTC, datetime

import asyncpg
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from adapter_kernel.context import RequestContext
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolRequest, ToolResult, ToolSuccess
from adapter_kernel.tooldef import Behavior
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.observability.adapters.otel import OtelTelemetry
from adapter_verify.observability.adapters.postgres import PostgresEventStore
from adapter_verify.observability.buffered_sink import BufferedEventSink
from adapter_verify.observability.domain.attributes import Outcome, SpanAttributes, SpanName
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.fakes import MemoryDiagnostics, MemoryPayloadStore
from adapter_verify.observability.ports import EventSink, PayloadKind, Telemetry
from adapter_verify.observability.service import META_CORRELATION_ID, InboundCall, ObservedEntry
from tests.sentinels import SENTINEL_PREFIX, sentinel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# §8.2 order, inside tool.call.
INNER_STAGES = (
    SpanName.ACCESS_CHECK,
    SpanName.LIFECYCLE_GATE,
    SpanName.INPUT_VALIDATE,
    SpanName.IDEMPOTENCY_RESERVE,
    SpanName.BACKEND_CALL,
    SpanName.DRIFT_ABSORB,
    SpanName.OUTPUT_VALIDATE,
)

UPSTREAM_BODY: JsonObject = {
    "order_id": "88213",
    "status": "IN_PROGRESS",
    "customer": {"name": sentinel("name"), "phone": sentinel("phone")},
}


class FakePipeline:
    def __init__(
        self,
        telemetry: Telemetry,
        events: EventSink,
        payloads: MemoryPayloadStore,
        clock: ManualClock,
    ) -> None:
        self._telemetry = telemetry
        self._events = events
        self._payloads = payloads
        self._clock = clock

    async def __call__(
        self,
        ctx: RequestContext,
        request: ToolRequest,
        credential: SecretStr | None,  # noqa: ARG002 - part of the Downstream protocol
    ) -> ToolResult:
        for stage in INNER_STAGES:
            with self._telemetry.span(
                stage, SpanAttributes(correlation_id=ctx.correlation_id)
            ) as span:
                self._clock.advance(0.01)
                ref = None
                if stage is SpanName.BACKEND_CALL:
                    ref = await self._payloads.put(
                        ctx.correlation_id, PayloadKind.UPSTREAM_RESPONSE, UPSTREAM_BODY
                    )
                self._events.emit(
                    CallEvent(
                        timestamp=self._clock.now(),
                        trace_id=span.trace_id,
                        span_id=span.span_id,
                        correlation_id=ctx.correlation_id,
                        agent_id="synthetic-agent",
                        tool=ctx.tool,
                        semantic_version="1.2.0",
                        stage=stage,
                        behavior=Behavior.READ_ONLY,
                        outcome=Outcome.OK,
                        payload_ref=ref,
                    )
                )
        return ToolResult(
            outcome=ToolSuccess(
                content=request.arguments, meta=ResponseMeta(correlation_id=ctx.correlation_id)
            )
        )


async def test_one_correlation_id_returns_every_step_in_order_without_leaking(
    pool: asyncpg.Pool,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = OtelTelemetry(provider.get_tracer("acceptance"))
    clock = ManualClock(datetime(2026, 6, 1, 9, 0, tzinfo=UTC))
    diagnostics = MemoryDiagnostics()
    store = PostgresEventStore(pool)
    buffer = BufferedEventSink(store, diagnostics, capacity=100, batch_size=3)
    payloads = MemoryPayloadStore()
    entry = ObservedEntry(
        telemetry=telemetry,
        events=buffer,
        diagnostics=diagnostics,
        clock=clock,
        entropy=SeededEntropy(b"acceptance"),
        downstream=FakePipeline(telemetry, buffer, payloads, clock),
    )

    result = await entry.handle(
        InboundCall(
            tool="order.get",
            arguments={"order_id": "88213"},
            meta={META_CORRELATION_ID: "job-42"},
        )
    )
    await buffer.flush()
    assert isinstance(result.outcome, ToolSuccess)

    # Every step, in order, from one lookup.
    trace = await store.by_correlation_id("job-42")
    assert [e.stage for e in trace] == [*INNER_STAGES, SpanName.TOOL_CALL]
    assert len({e.trace_id for e in trace}) == 1

    # Spans form one tree under tool.call.
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == SpanName.TOOL_CALL.value)
    children = [s for s in spans if s is not root]
    assert {s.name for s in children} == {s.value for s in INNER_STAGES}
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in children)

    # The body is retrievable from the payload store via the logged ref, and nowhere else.
    (backend_event,) = [e for e in trace if e.stage is SpanName.BACKEND_CALL]
    assert backend_event.payload_ref is not None
    assert await payloads.get(backend_event.payload_ref) == UPSTREAM_BODY
    stored_rows = await pool.fetch("SELECT event::text AS event FROM call_events")
    assert all(SENTINEL_PREFIX not in r["event"] for r in stored_rows)
    assert diagnostics.internal_errors == []
    # Spans and diagnostics are also scanned by the sentinel plugin after this test.
    provider.shutdown()
