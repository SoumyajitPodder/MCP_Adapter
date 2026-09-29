"""Rendering of a correlation-ID trace for ``adapter-verify trace`` (brief §8.9)."""

import json
from collections.abc import Sequence

from adapter_verify.observability.domain.events import CallEvent

_COLUMNS = ("time", "stage", "outcome", "error", "tool", "agent", "backend", "ms", "payload")


def _row(event: CallEvent) -> tuple[str, ...]:
    tool = (
        event.tool if event.semantic_version is None else f"{event.tool}@{event.semantic_version}"
    )
    return (
        event.timestamp.isoformat(timespec="milliseconds"),
        event.stage.value,
        event.outcome.value,
        event.error_code.value if event.error_code else "-",
        tool,
        event.agent_id or "-",
        event.backend or "-",
        "-" if event.latency_ms is None else f"{event.latency_ms:.1f}",
        event.payload_ref or "-",
    )


def render_table(events: Sequence[CallEvent]) -> str:
    rows = [_COLUMNS, *(_row(e) for e in events)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def render_json(events: Sequence[CallEvent]) -> str:
    return json.dumps([e.model_dump(mode="json") for e in events], indent=2, sort_keys=True)
