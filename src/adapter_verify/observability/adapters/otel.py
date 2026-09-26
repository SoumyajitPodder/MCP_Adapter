"""Telemetry port implemented with the OpenTelemetry SDK."""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from adapter_kernel.errors import ErrorCode
from adapter_verify.observability.domain.attributes import (
    SEMCONV_GENAI_COMMIT,
    SpanAttributes,
    SpanName,
    otel_attributes,
)
from adapter_verify.settings import ObservabilitySettings


class _OtelSpan:
    def __init__(self, span: Span, name: SpanName) -> None:
        self._span = span
        self._name = name

    @property
    def trace_id(self) -> str:
        return format(self._span.get_span_context().trace_id, "032x")

    @property
    def span_id(self) -> str:
        return format(self._span.get_span_context().span_id, "016x")

    def update(self, attrs: SpanAttributes) -> None:
        self._span.set_attributes(otel_attributes(self._name, attrs))

    def fail(self, code: ErrorCode) -> None:
        self._span.set_attribute("adapter.error_code", code.value)
        self._span.set_status(Status(StatusCode.ERROR, code.value))


class OtelTelemetry:
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    @contextmanager
    def span(self, name: SpanName, attrs: SpanAttributes) -> Iterator[_OtelSpan]:
        # Exception recording is off: the SDK would copy the exception message, which can
        # contain customer data, into span events and the status description.
        with self._tracer.start_as_current_span(
            name.value,
            attributes=otel_attributes(name, attrs),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield _OtelSpan(span, name)


def build_tracer_provider(settings: ObservabilitySettings) -> TracerProvider:
    """Tracer provider with a bounded, batched, async OTLP export (brief §8.8).

    With no endpoint configured, spans are created but not exported.
    """
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "adapter.semconv_genai_commit": SEMCONV_GENAI_COMMIT,
            }
        )
    )
    if settings.otlp_endpoint is not None:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otlp_endpoint),
                max_queue_size=settings.span_queue_size,
            )
        )
    return provider
