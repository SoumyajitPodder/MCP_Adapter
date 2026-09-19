"""
mcp_server.py

The MCP layer. This is what the pipeline exposes as a "stable" tool-call
interface -- in v1, it is intentionally NOT stable yet in the durability
sense (no versioning, no semantic validation). It's a clean tool registry
and dispatch mechanism, which is the scaffolding those features will sit
on top of.

WHAT THIS LAYER WILL EVENTUALLY OWN (not implemented in v1):
    - Versioned tool contracts (v1/v2 coexistence, translation shims)
        -> EXTENSION POINT: see TOOL_REGISTRY, would become
           TOOL_REGISTRY["get_customer_info"]["v1"] / ["v2"]
    - Semantic/schema validation of inbound requests and outbound
      responses against a promised contract
        -> EXTENSION POINT: see call_tool(), marked below
    - Golden-task regression hooks (replaying known-good transcripts
      whenever a tool definition changes)
        -> lives alongside this layer, not inside it; not built yet

WHAT THIS LAYER OWNS TODAY:
    - A registry of available tools and their (unvalidated) input shape
    - Dispatch: routes a tool call to the correct driver via the adapter
    - list_tools(): lets an agent discover what's available, the
      "semantic discovery" behavior described in the original design doc
      (what tools exist, what do you need from me) -- present here in
      its simplest possible form
"""

from adapter.api_driver import ATTApiDriver
from adapter.base_driver import AdapterError

TOOL_REGISTRY = {
    "check_device_inventory": {
        "description": "Check available stock for a device SKU.",
        "input_schema": {"sku": "string, e.g. 'SKU-IP15PM'"},
        "driver": "api",
    },
    "get_service_ticket_status": {
        "description": "Look up the status of a service/repair ticket.",
        "input_schema": {"ticket_id": "string, e.g. 'TICKET-5001'"},
        "driver": "api",
    },
    "get_order_status": {
        "description": "Get status, carrier, and tracking for a device order.",
        "input_schema": {"order_id": "string, e.g. 'ORDER-8001'"},
        "driver": "api",
    },
}


class MCPToolError(Exception):
    """Raised when a tool call can't be fulfilled, for any reason."""


class MCPServer:
    """
    Minimal MCP-style server: tool discovery + dispatch.
    One driver instance per driver type. v1 only wires up the API driver.
    """

    def __init__(self):
        self._drivers = {
            "api": ATTApiDriver(),
            # EXTENSION POINT: future driver types register here, e.g.
            # "db": ATTDbDriver(), "file": ATTFileDriver(), "queue": ATTQueueDriver()
        }

    def list_tools(self) -> dict:
        """What an agent asks first: 'what tools exist?'"""
        return TOOL_REGISTRY

    def call_tool(self, tool_name: str, params: dict, trace=None) -> dict:
        print(f"    [mcp]     received call_tool('{tool_name}', {params})")
        if trace:
            trace.step("mcp", "received_call", {"tool_name": tool_name, "params": params})

        tool = TOOL_REGISTRY.get(tool_name)
        if not tool:
            if trace:
                trace.step("mcp", "unknown_tool", {"tool_name": tool_name}, status="error")
            raise MCPToolError(f"no such tool: '{tool_name}'")

        # EXTENSION POINT (semantic layer): inbound validation would go
        # here -- check `params` against tool["input_schema"] as a real
        # schema, not just a docstring, before dispatching.

        driver = self._drivers.get(tool["driver"])
        if not driver:
            if trace:
                trace.step("mcp", "no_driver", {"driver_type": tool["driver"]}, status="error")
            raise MCPToolError(f"no driver registered for type '{tool['driver']}'")

        if trace:
            trace.step("mcp", "dispatch", {"driver_type": tool["driver"]})

        try:
            result = driver.call(tool_name, params, trace=trace)
        except AdapterError as exc:
            print(f"    [mcp]     driver error: {exc}")
            raise MCPToolError(str(exc)) from exc

        # EXTENSION POINT (semantic layer): outbound normalization would
        # go here -- validate `result` against the promised output shape
        # and normalize it before returning, regardless of which driver
        # or backend version actually produced it.

        print(f"    [mcp]     returning result from driver")
        if trace:
            trace.step("mcp", "result_returned", {"fields": list(result.keys())})
        return result
