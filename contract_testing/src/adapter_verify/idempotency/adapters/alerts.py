"""Owner alerts as structured log lines. Interim until the alerting channel is chosen.

TODO(owner): route to the tool owner's paging or ticketing channel (§7.8).
"""

import logging

from adapter_verify.idempotency.domain.records import AlertKind, RecordKey


class LogOwnerAlerts:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def alert(self, kind: AlertKind, key: RecordKey, correlation_id: str) -> None:
        self._logger.warning(
            "idempotency_alert",
            extra={
                "event": {"alert": kind.value, "agent_id": key.agent_id, "tool": key.tool},
                "correlation_id": correlation_id,
            },
        )
