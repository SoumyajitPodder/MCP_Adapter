# MCP Operations Sandbox (v1) — AT&T-flavored

A minimal, working pipeline that demonstrates:

```
Agent  →  Gateway  →  MCP  →  Adapter (API driver)  →  Mock AT&T endpoint
```

Covering three domains: **inventory**, **service tickets**, and **order
management** — a deliberate shift away from customer/billing lookups
toward operational agent workflows.

**Scope of this version, on purpose:**
- Only the **API driver** is implemented. DB, file, queue, and RPA drivers
  come later, once this path is proven.
- **None** of the durability features from the design discussion are built
  in yet — no versioned contracts, no semantic validation, no contract
  testing, no golden-task regression, no idempotency keys, no credential
  scoping. This is the honest, unprotected baseline.
- Every layer has clearly marked `EXTENSION POINT` comments showing
  exactly where each of those features will be added, so wiring them in
  later doesn't require restructuring anything.

---

## Why it's built this way

Each layer is its own Python module with a narrow, single job:

| Layer | File | Job today |
|---|---|---|
| Agent | `agent/agent.py` | Decide which tool a task needs (toy keyword matching — a real LLM replaces only this) |
| Gateway | `gateway/gateway.py` | Route the call to the right MCP server (only one exists: AT&T) |
| MCP | `mcp/mcp_server.py` | Tool registry + dispatch to the right driver |
| Adapter | `adapter/api_driver.py` | Translate a tool call into an HTTP request |
| Endpoint | `endpoint/mock_att_api.py` | Simulated AT&T legacy REST backend — inventory, service tickets, orders |

The layers are separate classes/modules invoked in-process (not separate
network services) — the only real network hop is the adapter calling out
to the mock endpoint over HTTP. That mirrors the "keep the semantic layer
embedded, not a separate hop" decision from earlier: logical separation
without paying for physical hops you don't need yet.

`adapter/base_driver.py` defines the interface every driver must satisfy.
Right now `ATTApiDriver` is the only one. When DB/file/queue/RPA
integration paths get built, they implement the same interface and
register in `MCPServer._drivers` — nothing above the adapter layer has
to change.

**Tools available today:**

| Tool | Domain | Backend route |
|---|---|---|
| `check_device_inventory` | Inventory | `GET /api/v1/inventory/<sku>` |
| `get_service_ticket_status` | Service | `GET /api/v1/service-tickets/<ticket_id>` |
| `get_order_status` | Order management | `GET /api/v1/orders/<order_id>` |

Demo IDs baked into the mock backend: `SKU-IP15PM`, `SKU-GS24U`,
`SKU-PXL9PR` · `TICKET-5001`, `TICKET-5002`, `TICKET-5003` ·
`ORDER-8001`, `ORDER-8002`, `ORDER-8003`.

---

## Running it — CLI

```bash
pip install -r requirements.txt --break-system-packages

# terminal 1 — start the mock legacy backend
python endpoint/mock_att_api.py

# terminal 2 — run the pipeline against it
python demo/run_demo.py
```

You'll see each layer log as a request passes through it:

```
[agent]       task: "What's the status on this repair ticket?"
[agent]       decided to call tool 'get_service_ticket_status' with {'ticket_id': 'TICKET-5001'}
  [gateway]   routing call_tool('get_service_ticket_status') -> MCP
    [mcp]     received call_tool('get_service_ticket_status', {...})
    [mcp]     returning result from driver
  [gateway]   received result, passing back to agent
[agent]       got result back, task complete
```

---

## Running it — web UI

```bash
# terminal 1
python endpoint/mock_att_api.py

# terminal 2
python web/app.py
```

Then open **http://127.0.0.1:5050**.

The page has two panels:

- **Conversation** (left) — a normal chat interface. Type a task in plain
  language, or click one of the example chips. A small regex layer in
  `web/app.py` (`extract_ids`) pulls SKU/ticket/order IDs out of your
  text, standing in for the slot-filling a real LLM agent would do.
- **System internals** (right) — a live, per-request trace of the exact
  pipeline hop sequence: agent reasoning → gateway routing → MCP dispatch
  → adapter call (with the **API version** that served it) → backend
  response, each with elapsed time and status. Below that, an **MCP layer
  capabilities** checklist showing what's implemented vs. planned.

The trace comes from `common/trace.py`, a small structured tracer passed
as an optional `trace=` argument through every layer
(`Agent.handle_task` → `Gateway.call_tool` → `MCPServer.call_tool` →
`ATTApiDriver.call`). The CLI demo doesn't pass a trace, so it's
completely unaffected — the web UI is just a second way of observing the
same underlying pipeline code.

### Keeping the capabilities panel current

`CAPABILITIES` in `web/app.py` is the single source of truth for the
right-hand checklist. As each roadmap item below gets built, flip its
`"status"` from `"planned"` to `"implemented"` — the UI updates with no
frontend changes. When a request uses the API driver, its list entry
briefly highlights, so the panel doubles as a live "what's actually
running vs. what's still on paper" view — and that same highlight
mechanism will surface future capabilities (e.g. "contract validation
passed") the moment they start emitting trace steps.

### Seeing the problem this whole project exists to solve

The mock endpoint supports an `ATT_MOCK_DRIFT_MODE` env var to simulate a
legacy backend changing shape underneath the pipeline — useful later for
testing contract-testing/semantic-layer work against a real signal.

```bash
ATT_MOCK_DRIFT_MODE=rename python endpoint/mock_att_api.py
```

Re-run a task (CLI or web UI) and watch `quantity_available` silently
become `qty_avail`, `status` become `current_status`, and
`tracking_number` become `tracking_id` — with nothing in the pipeline
catching it. In the web UI this is easiest to see in the trace panel's
`backend_response` step, where the `fields` list visibly changes shape.
That gap — currently open — is exactly what v1's five prioritized
capabilities exist to close.

Other modes: `delay` (simulates a degrading backend, for future gateway
fallback work) and `error` (~30% random 500s, for future
retry/circuit-breaker work).

---

## Roadmap — where the next five capabilities plug in

These are the five capabilities selected as the v1 priority list, and
exactly where each one lands in this codebase:

1. **Versioned tool contracts** → `mcp/mcp_server.py`, `TOOL_REGISTRY`.
   Becomes version-keyed (`TOOL_REGISTRY["get_order_status"]["v1"]`,
   `["v2"]`) instead of flat.
2. **Absorb drift, guarantee stable output shape** → the two
   `EXTENSION POINT` comments in `MCPServer.call_tool()` (inbound
   validation before dispatch, outbound normalization after).
3. **Multiple tool versions side by side (shims)** → `adapter/` gains a
   second driver per version, or `api_driver.py` gains version-aware
   translation; `Gateway` gains the routing decision of which version to
   call, per the healthy → tested → canary → primary → deprecated →
   retired lifecycle.
4. **Contract testing in CI** → new, sits outside the runtime pipeline
   entirely — a CI job that diffs the mock endpoint's actual response
   shape against `TOOL_REGISTRY`'s promised shape on every change.
5. **Golden-task regression suite** → new, sits alongside `demo/` — a
   set of recorded task → expected-result pairs replayed against the
   pipeline whenever a tool definition changes, to catch semantic drift
   (not just schema drift).

Deferred past v1 (see prior discussion for reasoning): idempotency keys,
scoped least-privilege credentials, correlation-ID tracing, dependency
mapping, live-traffic evaluation, and gateway-level retries/circuit
breakers/fallback tiers.

Every item above already has a row in `web/app.py`'s `CAPABILITIES` list
— flip its status when it ships and the dashboard shows it immediately.
