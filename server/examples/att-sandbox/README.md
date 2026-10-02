# Example: the MCP sandbox, served through the controller

`config.json` registers the sandbox's three tools (`check_device_inventory`, `get_service_ticket_status`,
`get_order_status`) with contracts matching the sandbox's own canonical schemas, reading the sandbox's mock
backend over HTTP.

* `att_sandbox_driver.py` — a `BackendDriver` for the sandbox's own seam. `install(gateway, client)` registers it at the
  sandbox's `EXTENSION POINT` (`MCPServer._drivers`); none of the sandbox's code changes.
* `e2e_demo.py` — drives every drift mode of the mock backend through the real server with the Python client.
* `sandbox_pipeline_demo.py` — the sandbox's `Agent` pipeline, unchanged, through a drift event and an approval.
* `demo.sh` — starts the mock and the server as separate processes and runs both:

```bash
SANDBOX_ROOT=/path/to/mcp_sandbox bash demo.sh
```
