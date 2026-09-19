"""
base_driver.py

Defines the interface every backend driver must implement.

Today only ATTApiDriver (api_driver.py) exists, because we're deliberately
scoping v1 of this sandbox to the API integration path only.

FUTURE DRIVERS (not built yet, this is the seam they'll plug into):
    - att_db_driver.py     direct DB reads/writes against a legacy database
    - att_file_driver.py   batch/file-drop based integration
    - att_queue_driver.py  message queue / event bus bridging
    - att_rpa_driver.py    UI automation / screen-scraping for systems
                            with no programmatic interface at all

Whatever driver type ends up serving a given tool call, it must return
data in the SAME shape for the same tool. That's what lets the MCP/agent
layers stay backend-agnostic later -- the driver is the only place that
knows whether the data came from an API, a database, a file, or a queue.
"""

from abc import ABC, abstractmethod


class BackendDriver(ABC):
    """Common interface for every backend integration type."""

    @abstractmethod
    def call(self, tool_name: str, params: dict, trace=None) -> dict:
        """
        Execute a tool call against this driver's backend and return a
        plain dict result.

        Must raise AdapterError (see below) on any failure rather than
        letting backend-specific exceptions leak upward -- the MCP layer
        should never need to know whether it's talking to a driver backed
        by requests.exceptions.* vs psycopg2.* vs a file-not-found error.

        `trace` is an optional common.trace.Trace instance. When present,
        implementations should call trace.step("adapter", ...) around the
        actual backend call so the web UI can show what happened (e.g.
        which API version, DB, file, or queue actually served the
        request) without needing driver-specific UI code.
        """
        raise NotImplementedError


class AdapterError(Exception):
    """
    Raised by any driver when a backend call fails, regardless of backend
    type. Carries enough detail for the gateway layer to eventually decide
    on retries/fallback (not implemented yet in v1).
    """

    def __init__(self, message: str, tool_name: str = None, status_code: int = None):
        super().__init__(message)
        self.tool_name = tool_name
        self.status_code = status_code
