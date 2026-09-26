from datetime import UTC, datetime

import pytest

from adapter_kernel.errors import ErrorCode
from adapter_verify.common.fakes import SeededEntropy
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.fakes import MemoryEventSink
from adapter_verify.observability.service import SampledEventSink

pytestmark = pytest.mark.unit


def _event(outcome: Outcome = Outcome.OK, code: ErrorCode | None = None) -> CallEvent:
    return CallEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        trace_id="t",
        span_id="s",
        correlation_id="c",
        agent_id=None,
        tool="order.get",
        semantic_version=None,
        stage=SpanName.TOOL_CALL,
        outcome=outcome,
        error_code=code,
    )


def test_sampled_sink_drops_reads_only() -> None:
    inner = MemoryEventSink()
    sink = SampledEventSink(inner, read_ratio=0.0, entropy=SeededEntropy(b"s"))
    sink.emit(_event())
    sink.emit(_event(Outcome.ERROR, ErrorCode.INTERNAL))
    assert [e.outcome for e in inner.events] == [Outcome.ERROR]


def test_sampled_sink_keeps_everything_at_ratio_one() -> None:
    inner = MemoryEventSink()
    sink = SampledEventSink(inner, read_ratio=1.0, entropy=SeededEntropy(b"s"))
    for _ in range(50):
        sink.emit(_event())
    assert len(inner.events) == 50


def test_sampled_sink_keeps_roughly_the_ratio() -> None:
    inner = MemoryEventSink()
    sink = SampledEventSink(inner, read_ratio=0.3, entropy=SeededEntropy(b"s"))
    for _ in range(2000):
        sink.emit(_event())
    assert 500 < len(inner.events) < 700
