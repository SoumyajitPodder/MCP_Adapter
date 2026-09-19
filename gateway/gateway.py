"""
gateway.py

The Gateway layer. Sits between the agent and MCP. In a real deployment
this is where cross-cutting operational concerns live -- in v1, those are
present only as clearly marked stubs, not implemented.

WHAT THIS LAYER WILL EVENTUALLY OWN (not implemented in v1):
    - Retries with backoff on transient failures
    - Circuit breaking (stop hammering a failing backend)
    - Fallback tiers (cache / degraded estimate / queued response)
    - Auth to upstream systems, scoped per agent/tool
    - Version selection (routing a call to the right tool version,
      supporting the lifecycle: healthy -> tested -> canary -> primary
      -> deprecated -> retired)
    - Correlation-ID stamping for cross-system tracing

WHAT THIS LAYER OWNS TODAY:
    - A single routing decision: which MCPServer instance handles this
      call (today there is only one: the AT&T MCP server)
    - Pass-through logging, so you can see the request cross this layer

Kept as a thin, separate module on purpose -- even though it does almost
nothing yet, giving it its own file/class now means the retry/circuit-
breaker/fallback logic added later has an obvious, uncontested home,
instead of getting bolted onto the agent or the MCP layer out of
convenience.
"""

from mcp.mcp_server import MCPServer, MCPToolError


class GatewayError(Exception):
    """Raised when the gateway can't route or fulfill a request."""


class Gateway:
    def __init__(self):
        # EXTENSION POINT: this becomes a routing table once there's more
        # than one backend system / MCP server to choose between, e.g.
        # { "att": MCPServer(...), "verizon": MCPServer(...) }
        self._mcp = MCPServer()

    def list_tools(self) -> dict:
        return self._mcp.list_tools()

    def call_tool(self, tool_name: str, params: dict, trace=None) -> dict:
        print(f"  [gateway]   routing call_tool('{tool_name}') -> MCP")
        if trace:
            trace.step("gateway", "routing", {"tool_name": tool_name, "target": "att-mcp-server"})

        # EXTENSION POINT: retries/circuit-breaker would wrap the call
        # below. EXTENSION POINT: version selection would decide which
        # registered tool version to invoke before dispatch.

        try:
            result = self._mcp.call_tool(tool_name, params, trace=trace)
        except MCPToolError as exc:
            print(f"  [gateway]   MCP layer raised an error: {exc}")
            if trace:
                trace.step("gateway", "downstream_error", {"error": str(exc)}, status="error")
            raise GatewayError(str(exc)) from exc

        print(f"  [gateway]   received result, passing back to agent")
        if trace:
            trace.step("gateway", "result_received", {})
        return result
