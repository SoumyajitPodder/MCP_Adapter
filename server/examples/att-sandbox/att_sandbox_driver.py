"""A backend driver that serves the MCP sandbox's tools through the lifecycle
controller instead of straight from the legacy API.

It implements the sandbox's own seam (adapter.base_driver.BackendDriver) and
registers where the sandbox says future drivers go (MCPServer._drivers, the
EXTENSION POINT in mcp/mcp_server.py), so none of the sandbox's code changes:

    from att_sandbox_driver import install
    install(agent.gateway, LifecycleClient(URL, token=AGENT_TOKEN))

The driver returns canonical records. The sandbox's own drift layer then sees
clean, contract-shaped data and passes it straight through.
"""
from adapter.base_driver import AdapterError, BackendDriver
from lifecycle_client import LifecycleError


class LifecycleDriver(BackendDriver):
    def __init__(self, client):
        self.client = client

    def call(self, tool_name, params, trace=None):
        if trace:
            trace.step("adapter", "lifecycle_call", {"tool": tool_name, "params": params})
        try:
            out = self.client.call(tool_name, params)
        except LifecycleError as e:
            if trace:
                trace.step("adapter", "lifecycle_error", {"code": e.code, "review": e.review}, status="error")
            hint = f" (awaiting review {e.review})" if e.review else ""
            raise AdapterError(f"{e.code}: {e.message}{hint}") from e
        data = out["data"]
        return data[0] if len(data) == 1 else {"items": data}


def install(gateway, client, driver_type="api"):
    """Swap the gateway's MCP server over to the lifecycle driver. Returns the driver."""
    from mcp.mcp_server import MCPServer  # the sandbox's own package (named `mcp`)
    server = next(v for v in vars(gateway).values() if isinstance(v, MCPServer))
    server._drivers[driver_type] = LifecycleDriver(client)
    return server._drivers[driver_type]
