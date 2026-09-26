import json
from datetime import UTC, datetime

import pytest

from adapter_kernel.errors import ErrorCode
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.domain.trace import render_json, render_table

pytestmark = pytest.mark.unit

EVENTS = [
    CallEvent(
        timestamp=datetime(2026, 5, 1, 12, 0, 0, 1000, tzinfo=UTC),
        trace_id="t",
        span_id="s1",
        correlation_id="c",
        agent_id="order-status-agent",
        tool="order.get",
        semantic_version="1.2.0",
        stage=SpanName.ACCESS_CHECK,
        outcome=Outcome.OK,
    ),
    CallEvent(
        timestamp=datetime(2026, 5, 1, 12, 0, 1, tzinfo=UTC),
        trace_id="t",
        span_id="s2",
        correlation_id="c",
        agent_id=None,
        tool="order.get",
        semantic_version=None,
        stage=SpanName.BACKEND_CALL,
        backend="order-management",
        latency_ms=12.34,
        outcome=Outcome.ERROR,
        error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
        payload_ref="payload:abc",
    ),
]


def test_table_has_one_row_per_event_in_order() -> None:
    lines = render_table(EVENTS).splitlines()
    assert lines[0].split() == [
        "time",
        "stage",
        "outcome",
        "error",
        "tool",
        "agent",
        "backend",
        "ms",
        "payload",
    ]
    assert lines[1].split()[1:5] == ["access.check", "ok", "-", "order.get@1.2.0"]
    assert lines[2].split()[1:] == [
        "backend.call",
        "error",
        "UPSTREAM_UNAVAILABLE",
        "order.get",
        "-",
        "order-management",
        "12.3",
        "payload:abc",
    ]


def test_json_round_trips() -> None:
    restored = [
        CallEvent.model_validate_json(json.dumps(e)) for e in json.loads(render_json(EVENTS))
    ]
    assert restored == EVENTS
