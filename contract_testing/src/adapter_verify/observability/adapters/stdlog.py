"""Structured stdlib-logging adapters: events and diagnostics as JSON lines (brief §2).

Interim event sink until the Postgres event store lands (M1 delivery step 5).
"""

import json
import logging
from typing import Final

from adapter_verify.observability.domain.events import CallEvent

# Only these LogRecord extras are serialized; anything else attached to a record is ignored.
_STRUCTURED_FIELDS: Final = ("event", "correlation_id", "summary")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in _STRUCTURED_FIELDS:
            if name in record.__dict__:
                body[name] = record.__dict__[name]
        return json.dumps(body, sort_keys=True, default=str)


class LogEventSink:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(self, event: CallEvent) -> None:
        self._logger.info("call_event", extra={"event": event.model_dump(mode="json")})


class LogDiagnostics:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def internal_error(self, correlation_id: str, summary: str) -> None:
        self._logger.error(
            "internal_error", extra={"correlation_id": correlation_id, "summary": summary}
        )
