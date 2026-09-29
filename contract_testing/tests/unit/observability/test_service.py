from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import SecretStr

from adapter_kernel.context import RequestContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.observability.context import current_request
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.domain.ids import uuid7_unix_ms
from adapter_verify.observability.fakes import (
    MemoryDiagnostics,
    MemoryEventSink,
    RecordingTelemetry,
)
from adapter_verify.observability.service import (
    META_CORRELATION_ID,
    META_IDEMPOTENCY_KEY,
    META_TOOL_VERSION,
    InboundCall,
    ObservedEntry,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Harness:
    def __init__(self, result: ToolResult | Exception | None = None) -> None:
        self.telemetry = RecordingTelemetry()
        self.events = MemoryEventSink()
        self.diagnostics = MemoryDiagnostics()
        self.clock = ManualClock(_START)
        self.seen: list[RequestContext] = []
        self.visible: list[RequestContext | None] = []
        self._result = result
        self.entry = ObservedEntry(
            telemetry=self.telemetry,
            events=self.events,
            diagnostics=self.diagnostics,
            clock=self.clock,
            entropy=SeededEntropy(b"test"),
            downstream=self.downstream,
        )

    async def downstream(
        self,
        ctx: RequestContext,
        request: ToolRequest,
        credential: SecretStr | None,  # noqa: ARG002 - part of the Downstream protocol
    ) -> ToolResult:
        self.seen.append(ctx)
        self.visible.append(current_request())
        self.clock.advance(0.25)
        if isinstance(self._result, Exception):
            raise self._result
        if self._result is not None:
            return self._result
        return ToolResult(
            outcome=ToolSuccess(
                content=request.arguments, meta=ResponseMeta(correlation_id=ctx.correlation_id)
            ),
            delivery=DeliveryStatus.ACKED,
        )

    @property
    def event(self) -> CallEvent:
        assert len(self.events.events) == 1
        return self.events.events[0]


def _call(**meta: str) -> InboundCall:
    return InboundCall(tool="order.get", arguments={"order_id": "88213"}, meta=meta)


async def test_supplied_correlation_id_is_used_and_echoed() -> None:
    h = Harness()
    result = await h.entry.handle(_call(**{META_CORRELATION_ID: "order-88213"}))
    assert result.outcome.meta.correlation_id == "order-88213"
    assert h.seen[0].correlation_id == "order-88213"
    assert not h.seen[0].correlation_id_generated
    assert h.event.correlation_id == "order-88213"
    assert h.event.outcome is Outcome.OK


async def test_missing_correlation_id_gets_a_uuid7_from_the_clock() -> None:
    h = Harness()
    result = await h.entry.handle(_call())
    cid = result.outcome.meta.correlation_id
    assert UUID(cid).version == 7
    assert uuid7_unix_ms(UUID(cid)) == int(_START.timestamp() * 1000)
    assert h.seen[0].correlation_id_generated


async def test_invalid_correlation_id_is_rejected_without_running_downstream() -> None:
    h = Harness()
    result = await h.entry.handle(_call(**{META_CORRELATION_ID: "bad id\nwith newline"}))
    assert isinstance(result.outcome, ToolFailure)
    assert result.outcome.error.code is ErrorCode.INVALID_INPUT
    assert h.seen == []
    assert UUID(result.outcome.meta.correlation_id).version == 7
    assert h.event.error_code is ErrorCode.INVALID_INPUT
    assert "bad id" not in str(h.telemetry.spans[0].attributes)


async def test_context_carriers_are_read_from_meta() -> None:
    h = Harness()
    await h.entry.handle(_call(**{META_IDEMPOTENCY_KEY: "k-1", META_TOOL_VERSION: "1.2.0"}))
    assert h.seen[0].idempotency_key == "k-1"
    assert h.seen[0].requested_version == "1.2.0"
    assert h.event.idempotency_key == "k-1"


async def test_unsafe_idempotency_key_never_reaches_the_event() -> None:
    h = Harness()
    await h.entry.handle(_call(**{META_IDEMPOTENCY_KEY: "k 1\n"}))
    assert h.seen[0].idempotency_key == "k 1\n"
    assert h.event.idempotency_key is None


async def test_context_is_bound_during_downstream_only() -> None:
    h = Harness()
    await h.entry.handle(_call())
    assert h.visible == h.seen
    assert current_request() is None


async def test_span_and_event_share_ids_and_latency() -> None:
    h = Harness()
    await h.entry.handle(_call())
    (span,) = h.telemetry.spans
    assert span.name is SpanName.TOOL_CALL
    assert span.ended
    assert h.seen[0].trace_id == span.trace_id == h.event.trace_id
    assert h.event.span_id == span.span_id
    assert h.event.latency_ms == 250.0
    assert span.attributes["adapter.latency_ms"] == 250.0
    assert span.attributes["gen_ai.tool.name"] == "order.get"
    assert span.error_code is None


async def test_downstream_meta_correlation_id_is_overwritten() -> None:
    h = Harness(
        ToolResult(
            outcome=ToolSuccess(content={}, meta=ResponseMeta(correlation_id="other")),
            delivery=DeliveryStatus.ACKED,
        )
    )
    result = await h.entry.handle(_call(**{META_CORRELATION_ID: "mine"}))
    assert result.outcome.meta.correlation_id == "mine"


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (ErrorCode.NOT_AUTHORIZED, Outcome.DENIED),
        (ErrorCode.SCOPE_VIOLATION, Outcome.DENIED),
        (ErrorCode.CONTRACT_VIOLATION, Outcome.ERROR),
    ],
)
async def test_failures_map_to_outcomes(code: ErrorCode, outcome: Outcome) -> None:
    h = Harness(
        ToolResult(
            outcome=ToolFailure(
                error=AdapterError(code=code), meta=ResponseMeta(correlation_id="x")
            ),
            delivery=DeliveryStatus.ACKED,
        )
    )
    await h.entry.handle(_call())
    assert h.event.outcome is outcome
    assert h.event.error_code is code
    assert h.telemetry.spans[0].error_code is code


async def test_unexpected_exception_becomes_internal_without_leaking() -> None:
    sentinel = "SENTINEL-customer-5550001"
    h = Harness(RuntimeError(sentinel))
    result = await h.entry.handle(_call(**{META_CORRELATION_ID: "c-1"}))
    assert isinstance(result.outcome, ToolFailure)
    assert result.outcome.error.code is ErrorCode.INTERNAL
    ((cid, summary),) = h.diagnostics.internal_errors
    assert cid == "c-1"
    assert "builtins.RuntimeError" in summary
    for sink_text in (summary, str(result), str(h.events.events), str(h.telemetry.spans)):
        assert sentinel not in sink_text
