from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from adapter_kernel.classification import Classification
from adapter_kernel.errors import ErrorCode
from adapter_kernel.tooldef import Behavior
from adapter_verify.observability.domain.attributes import (
    Outcome,
    SpanAttributes,
    SpanName,
    otel_attributes,
)
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.domain.exceptions import safe_exception_summary
from adapter_verify.observability.domain.sampling import must_keep, should_keep

pytestmark = pytest.mark.unit


def _event(**overrides: Any) -> CallEvent:
    fields: dict[str, Any] = {
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "trace_id": "t",
        "span_id": "s",
        "correlation_id": "c",
        "agent_id": "synthetic-agent",
        "tool": "order.get",
        "semantic_version": "1.2.0",
        "stage": SpanName.TOOL_CALL,
        "outcome": Outcome.OK,
    }
    fields.update(overrides)
    return CallEvent(**fields)


def test_error_code_required_exactly_for_non_ok() -> None:
    with pytest.raises(ValidationError, match="error_code"):
        _event(outcome=Outcome.ERROR)
    with pytest.raises(ValidationError, match="error_code"):
        _event(error_code=ErrorCode.INTERNAL)
    assert _event(outcome=Outcome.DENIED, error_code=ErrorCode.NOT_AUTHORIZED)


def test_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _event(body="{}")


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": Outcome.ERROR, "error_code": ErrorCode.INTERNAL},
        {"outcome": Outcome.DENIED, "error_code": ErrorCode.NOT_AUTHORIZED},
        {"behavior": Behavior.MUTATING},
        {"behavior": Behavior.DESTRUCTIVE},
        {"drift_classification": Classification.COMPATIBLE},
        {"idempotency_state": "COMPLETED"},
    ],
)
def test_must_keep_events_are_never_sampled(overrides: dict[str, Any]) -> None:
    event = _event(**overrides)
    assert must_keep(event)
    assert should_keep(event, ratio=0.0, draw=0.99)


def test_successful_reads_follow_the_ratio() -> None:
    read = _event(behavior=Behavior.READ_ONLY)
    assert not must_keep(read)
    assert should_keep(read, ratio=0.5, draw=0.2)
    assert not should_keep(read, ratio=0.5, draw=0.7)
    assert should_keep(read, ratio=1.0, draw=0.999)


@pytest.mark.parametrize(("ratio", "draw"), [(-0.1, 0.5), (1.1, 0.5), (0.5, 1.0), (0.5, -0.1)])
def test_sampling_inputs_are_validated(ratio: float, draw: float) -> None:
    with pytest.raises(ValueError, match="must be within"):
        should_keep(_event(), ratio=ratio, draw=draw)


def test_tool_call_span_carries_adapter_and_mirrored_names() -> None:
    attrs = otel_attributes(
        SpanName.TOOL_CALL, SpanAttributes(tool="order.get", correlation_id="c", replayed=False)
    )
    assert attrs == {
        "adapter.tool": "order.get",
        "adapter.correlation_id": "c",
        "adapter.replayed": False,
        "gen_ai.operation.name": "execute_tool",
        "mcp.method.name": "tools/call",
        "gen_ai.tool.name": "order.get",
    }


def test_tool_call_without_tool_name_mirrors_operation_only() -> None:
    attrs = otel_attributes(SpanName.TOOL_CALL, SpanAttributes())
    assert "gen_ai.tool.name" not in attrs
    assert attrs["gen_ai.operation.name"] == "execute_tool"


def test_inner_spans_are_not_mirrored_and_omit_unset_fields() -> None:
    attrs = otel_attributes(
        SpanName.ACCESS_CHECK,
        SpanAttributes(outcome=Outcome.DENIED, rule_id="r1", error_code=ErrorCode.NOT_AUTHORIZED),
    )
    assert attrs == {
        "adapter.outcome": "denied",
        "adapter.rule_id": "r1",
        "adapter.error_code": "NOT_AUTHORIZED",
    }


def test_no_attribute_can_hold_a_body() -> None:
    with pytest.raises(ValidationError):
        SpanAttributes.model_validate({"arguments": {"ssn": "x"}})


def test_exception_summary_omits_the_message() -> None:
    sentinel = "sentinel-customer-4111"

    def fail() -> None:
        raise ValueError(sentinel)

    try:
        fail()
    except ValueError as exc:
        summary = safe_exception_summary(exc)
    assert sentinel not in summary
    assert summary.startswith("builtins.ValueError <- ")
    assert "in fail" in summary
