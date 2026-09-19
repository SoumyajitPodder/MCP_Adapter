"""
api_driver.py

The API driver: translates tool calls into HTTP requests against the mock
AT&T REST endpoint. This is the only driver implemented in v1 of the
sandbox -- DB, file, queue, and RPA drivers come later, once this path is
proven end to end.

WHAT THIS FILE DELIBERATELY DOES NOT DO YET (v1 scope):
    - No response validation against a promised contract
        -> future: semantic layer / contract enforcement
    - No versioning (v1/v2 shims) of the upstream API
        -> future: capability #3, version-keyed routing
    - No idempotency keys on writes
        -> not needed yet, v1 is read-only
    - No credential scoping / secret rotation
        -> future: capability #6
    - No retries / circuit breaking
        -> that's a GATEWAY concern, not an adapter concern, and the
           gateway is also a pass-through in v1 (see gateway/gateway.py)

Today, this driver is a straightforward, honest translator: agent-shaped
request in, one HTTP call out, raw response back. That simplicity is the
point -- it's the baseline you compare all the later hardening against.
"""

import requests

from adapter.base_driver import AdapterError, BackendDriver

ATT_API_BASE_URL = "http://127.0.0.1:5001/api/v1"


class ATTApiDriver(BackendDriver):
    """Talks to the mock AT&T legacy REST API over HTTP."""

    # Recorded in every trace step so the UI can show which upstream API
    # version actually served a call. Hard-coded to "v1" for now -- once
    # multi-version routing exists, this becomes per-call instead of
    # per-driver-instance.
    api_version = "v1"

    def __init__(self, base_url: str = ATT_API_BASE_URL, timeout_seconds: float = 5.0):
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def call(self, tool_name: str, params: dict, trace=None) -> dict:
        if tool_name == "check_device_inventory":
            return self._get(f"/inventory/{params['sku']}", tool_name, trace)

        if tool_name == "get_service_ticket_status":
            return self._get(f"/service-tickets/{params['ticket_id']}", tool_name, trace)

        if tool_name == "get_order_status":
            return self._get(f"/orders/{params['order_id']}", tool_name, trace)

        raise AdapterError(f"unknown tool '{tool_name}' for ATTApiDriver", tool_name=tool_name)

    def _get(self, path: str, tool_name: str, trace=None) -> dict:
        url = f"{self.base_url}{path}"

        if trace:
            trace.step(
                "adapter",
                "dispatch",
                {"driver": "api", "api_version": self.api_version, "url": url},
            )

        try:
            response = requests.get(url, timeout=self.timeout_seconds)
        except requests.exceptions.RequestException as exc:
            if trace:
                trace.step(
                    "adapter",
                    "backend_unreachable",
                    {"api_version": self.api_version, "url": url, "error": str(exc)},
                    status="error",
                )
            raise AdapterError(
                f"legacy backend unreachable: {exc}", tool_name=tool_name
            ) from exc

        if response.status_code != 200:
            if trace:
                trace.step(
                    "adapter",
                    "backend_error_response",
                    {
                        "api_version": self.api_version,
                        "status_code": response.status_code,
                        "body": response.text.strip(),
                    },
                    status="error",
                )
            raise AdapterError(
                f"legacy backend returned {response.status_code}: {response.text}",
                tool_name=tool_name,
                status_code=response.status_code,
            )

        result = response.json()
        if trace:
            trace.step(
                "adapter",
                "backend_response",
                {"api_version": self.api_version, "status_code": response.status_code, "fields": list(result.keys())},
            )
        return result
