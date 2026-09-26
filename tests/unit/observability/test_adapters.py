import json
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr, ValidationError

from adapter_kernel.errors import ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolRequest, ToolResult, ToolSuccess
from adapter_verify.composition import build_runtime
from adapter_verify.observability.adapters.otel import OtelTelemetry, build_tracer_provider
from adapter_verify.observability.adapters.stdlog import JsonFormatter, LogDiagnostics
from adapter_verify.observability.domain.attributes import (
    SEMCONV_GENAI_COMMIT,
    SpanAttributes,
    SpanName,
)
from adapter_verify.observability.service import InboundCall
from adapter_verify.settings import ObservabilitySettings

pytestmark = pytest.mark.unit


@pytest.fixture
def exporter() -> tuple[OtelTelemetry, InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return OtelTelemetry(provider.get_tracer("test")), memory


def test_otel_spans_nest_and_carry_typed_attributes(
    exporter: tuple[OtelTelemetry, InMemorySpanExporter],
) -> None:
    telemetry, memory = exporter
    with telemetry.span(SpanName.TOOL_CALL, SpanAttributes(tool="order.get")) as outer:
        with telemetry.span(SpanName.ACCESS_CHECK, SpanAttributes(rule_id="r1")) as inner:
            inner.fail(ErrorCode.NOT_AUTHORIZED)
        outer.update(SpanAttributes(latency_ms=3.0))
    inner_span, outer_span = memory.get_finished_spans()
    assert outer_span.name == "tool.call"
    assert outer_span.attributes is not None
    assert outer_span.attributes["gen_ai.tool.name"] == "order.get"
    assert outer_span.attributes["adapter.latency_ms"] == 3.0
    assert inner_span.parent is not None
    assert inner_span.parent.span_id == outer_span.context.span_id
    assert inner_span.status.status_code is StatusCode.ERROR
    assert inner_span.status.description == "NOT_AUTHORIZED"
    assert format(outer_span.context.trace_id, "032x") == outer.trace_id
    assert format(outer_span.context.span_id, "016x") == outer.span_id


def test_exception_messages_never_reach_spans(
    exporter: tuple[OtelTelemetry, InMemorySpanExporter],
) -> None:
    telemetry, memory = exporter
    sentinel = "SENTINEL-4111-1111"
    with (
        pytest.raises(ValueError, match=sentinel),
        telemetry.span(SpanName.BACKEND_CALL, SpanAttributes()),
    ):
        raise ValueError(sentinel)
    (span,) = memory.get_finished_spans()
    assert span.events == ()
    assert span.status.status_code is StatusCode.UNSET
    assert sentinel not in span.to_json()


def test_provider_records_semconv_pin_and_exports_only_when_configured() -> None:
    provider = build_tracer_provider(ObservabilitySettings())
    assert provider.resource.attributes["adapter.semconv_genai_commit"] == SEMCONV_GENAI_COMMIT
    provider.shutdown()
    configured = build_tracer_provider(
        ObservabilitySettings(otlp_endpoint="http://collector.invalid:4318/v1/traces")
    )
    configured.shutdown()


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_OBSERVABILITY_READ_SAMPLE_RATIO", "0.25")
    monkeypatch.setenv("ADAPTER_OBSERVABILITY_SPAN_QUEUE_SIZE", "512")
    settings = ObservabilitySettings()
    assert settings.read_sample_ratio == 0.25
    assert settings.span_queue_size == 512


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ADAPTER_OBSERVABILITY_READ_SAMPLE_RATIO", "1.5"),
        ("ADAPTER_OBSERVABILITY_SPAN_QUEUE_SIZE", "0"),
        ("ADAPTER_OBSERVABILITY_OTLP_ENDPOINT", "collector:4318"),
    ],
)
def test_invalid_settings_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        ObservabilitySettings()


def test_json_formatter_emits_only_known_fields() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "internal_error", None, None)
    record.__dict__.update({"correlation_id": "c", "summary": "s", "arguments": {"ssn": "x"}})
    body = json.loads(JsonFormatter().format(record))
    assert body == {
        "level": "ERROR",
        "logger": "x",
        "message": "internal_error",
        "correlation_id": "c",
        "summary": "s",
    }


def test_log_diagnostics_attach_structured_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.diagnostics")
    with caplog.at_level(logging.ERROR, logger="test.diagnostics"):
        LogDiagnostics(logger).internal_error("c-1", "builtins.RuntimeError")
    (record,) = caplog.records
    assert record.__dict__["correlation_id"] == "c-1"


@pytest.mark.asyncio
async def test_runtime_wires_a_working_entry(caplog: pytest.LogCaptureFixture) -> None:
    async def downstream(
        ctx: object, request: ToolRequest, credential: SecretStr | None
    ) -> ToolResult:
        del ctx, credential
        return ToolResult(
            outcome=ToolSuccess(content=request.arguments, meta=ResponseMeta(correlation_id="x"))
        )

    runtime = build_runtime(downstream, ObservabilitySettings())
    with caplog.at_level(logging.INFO, logger="adapter_verify.events"):
        result = await runtime.entry.handle(InboundCall(tool="order.get", arguments={"a": "1"}))
    runtime.shutdown()
    assert isinstance(result.outcome, ToolSuccess)
    assert [r.getMessage() for r in caplog.records] == ["call_event"]
