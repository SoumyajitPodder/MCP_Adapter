"""Sentinel scan plugin (DESIGN.md X-5, brief §8.10 / §9.10).

After every test, every observable sink created during the test is searched for the
sentinel prefix. A test that planted a sentinel in a customer-data field fails if the value
surfaced in a span, event, diagnostic, audit record or log line, even if the test itself
never looked there.

Scanned: in-memory telemetry, event sinks and stores, diagnostics, audit stores, OTel
in-memory span exporters, and every log record. Not scanned: the payload store, whose job
is to hold bodies. Opt out per test with ``@pytest.mark.sentinel_exempt`` (say why).
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapter_verify.observability import fakes

SENTINEL_PREFIX = "SENTINEL-"

_SCANNED: tuple[type[Any], ...] = (
    fakes.RecordingTelemetry,
    fakes.MemoryEventSink,
    fakes.MemoryEventStore,
    fakes.MemoryDiagnostics,
    fakes.MemoryAuditStore,
    InMemorySpanExporter,
)


def sentinel(name: str) -> str:
    """A value that must never reach an observable sink."""
    return f"{SENTINEL_PREFIX}{name}"


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(f"{record.getMessage()} {record.__dict__!r}")


def _contents(sink: Any) -> str:
    if isinstance(sink, InMemorySpanExporter):
        return "\n".join(span.to_json() for span in sink.get_finished_spans())
    return repr(vars(sink))


@pytest.fixture(autouse=True)
def _sentinel_scan(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("sentinel_exempt"):
        yield
        return

    created: list[Any] = []
    for cls in _SCANNED:
        original = cls.__init__

        def tracking_init(self: Any, *args: Any, __original: Any = original, **kw: Any) -> None:
            __original(self, *args, **kw)
            created.append(self)

        monkeypatch.setattr(cls, "__init__", tracking_init)

    capture = _LogCapture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)

    leaks = [type(s).__name__ for s in created if SENTINEL_PREFIX in _contents(s)]
    if any(SENTINEL_PREFIX in text for text in capture.texts):
        leaks.append("log records")
    if leaks:
        pytest.fail(f"sentinel value reached an observable sink: {', '.join(leaks)}")
