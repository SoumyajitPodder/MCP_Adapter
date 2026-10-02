# lifecycle-controller-server

An HTTP, SSE and **MCP** front for the [lifecycle controller](../controller/README.md). It gives an agent environment a *stable tool* in front of an unstable system, and gives people the controls to keep it that way.

```
 agents ──MCP (POST /mcp) or REST──┐
                                   ▼
                          ┌─────────────────┐   connectors   ┌───────────────────────┐
  people ──REST / SSE────▶│  this server    │───────────────▶│ legacy API · DB · file │
  (admin token)           │  + controller   │  (any system)  │ · your own service     │
                          └─────────────────┘                └───────────────────────┘
```

An agent calls `get_order_status({order_id})` and gets the same shape every time. If the legacy system changes, the change is **absorbed** (when it's safe and deterministic), **sent to a person with a recommendation** (when it's ambiguous), or **the call fails closed with an explanation** (when it can't be handled). The agent is never handed a guess.

**Agents and people are different roles, on purpose.** An `agent` token can read tool definitions and call tools. Only an `admin` token can approve a review, start a migration, or change a binding — so an agent can never approve its own fix. A test enforces this against every admin route.

## Quick start

```bash
export LC_AGENT_TOKEN=...   # 16+ characters. Tokens come from the environment, never from config files.
export LC_ADMIN_TOKEN=...
node server/bin/lifecycle-server.js --config my.config.json

curl -H "Authorization: Bearer $LC_AGENT_TOKEN" http://127.0.0.1:8787/v1/tools
curl -H "Authorization: Bearer $LC_AGENT_TOKEN" -H 'Content-Type: application/json' \
     -d '{"args":{"order_id":"ORDER-8001"}}' http://127.0.0.1:8787/v1/tools/get_order_status/call
```

For local development only: `--allow-anonymous` (no tokens; refuses any non-loopback host).

Programmatic use: `createLifecycleServer({ tokens, connectors, bindings, stateFile, ... })` from `src/index.js`.

## Using it from an agent environment

**As an MCP server** (any MCP client): point it at `http://HOST:8787/mcp` with the header `Authorization: Bearer <agent token>`. It speaks Streamable HTTP, stateless, JSON responses, and exposes one read-only tool per binding. Each tool's `outputSchema` **is the canonical contract**, so the client knows the stable shape up front. It is tested against the official MCP SDK client. Control-plane operations are deliberately *not* MCP tools.

**As REST** from anything else: `GET /v1/tools`, `POST /v1/tools/{name}/call`. The full API is at `GET /openapi.json` (generated from the route table the server dispatches on, so it cannot drift from behaviour).

**From Python or JS:** `clients/python/lifecycle_client.py` (standard library only) and `clients/js/client.js`.

**Into an existing agent stack:** `examples/att-sandbox/` shows replacing a legacy driver with the controller at an existing seam without changing the host's code.

### What a call returns

Success: `{ "data": [ {...canonical record...} ], "_meta": { "contract": "get_order_status@1.0.0", "served_by": "v1 (mapping v2)", "absorbed": [...], "warnings": [...] } }`

| `error.code` | HTTP | Meaning | A caller should |
|---|---|---|---|
| `NOT_FOUND` | 404 | No record for those arguments. An *answer*, not a fault: nothing is logged, health is untouched | not retry |
| `BAD_REQUEST` | 400 | Arguments failed the tool's input schema (or the upstream rejected them). Never reaches the upstream | fix the arguments |
| `DRIFT_BLOCKED` | 502 | The upstream changed in a way that can't be absorbed safely. `error.review` names the review a person needs to look at | **not** retry in a loop; surface it |
| `UPSTREAM_UNAVAILABLE` | 503 + `Retry-After` | An outage. Not drift: no review is opened | retry with backoff |
| `CONTRACT_SUNSET` | 410 | The tool has been retired | stop calling it |
| `RATE_LIMITED` | 429 + `Retry-After` | `maxConcurrentCalls` reached | retry shortly |
| `NO_SUCH_TOOL` | 404 | | |

Over MCP the same bodies arrive as tool results with `isError: true`.

## Configuration

```jsonc
{
  "server": { "host": "127.0.0.1", "port": 8787, "stateFile": "./state.json",
              "batchIntervalSeconds": 300, "maxConcurrentCalls": 64, "bodyLimitBytes": 1048576,
              "corsOrigins": [], "allowAnonymous": false },
  "tokens": [ { "name": "agent-prod", "role": "agent", "tokenEnv": "LC_AGENT_TOKEN" },
              { "name": "ops-alice",  "role": "admin", "tokenEnv": "LC_ADMIN_TOKEN" } ],
  "connectors": { "legacy": { "type": "http-json", "headers": { "Authorization": "env:LEGACY_API_KEY" }, "timeoutMs": 8000 } },
  "bindings": [ { "id": "get_order_status", "connector": "legacy", "contract": {...}, "inputs": {...}, "source": {...} } ]
}
```

* **Secrets** are never in the file: tokens use `tokenEnv`, and any connector header value written `env:NAME` is read from the environment when the connector is built. Binding `source` is stored in the state file, so keep secrets out of it.
* **Bindings in config are seeds.** A binding already in the persisted state keeps everything it has learned (approved mappings, audit trail); one whose upstream is down at startup is retried every 30 s instead of requiring a restart.
* The **first batch runs before the server reports ready**, so no binding reports `PENDING` and a restart picks up whatever changed while it was down.

### A binding

```jsonc
{ "id": "get_order_status", "displayName": "Order status", "description": "Status, carrier and tracking for an order.",
  "connector": "legacy",
  "contract": { "version": "1.0.0", "fields": [
      { "name": "order_id", "type": "string" },
      { "name": "status", "type": "enum", "values": ["processing", "shipped", "delivered", "backordered"] },
      { "name": "quantity", "type": "integer" },
      { "name": "tracking_number", "type": "string", "optional": true },
      { "name": "expected_delivery", "type": "date", "optional": true } ] },
  "inputs": { "properties": { "order_id": { "type": "string", "pattern": "^ORDER-[0-9]{1,10}$" } }, "required": ["order_id"] },
  "source": { "versions": { "v1": {
      "probes": ["https://legacy/api/orders/ORDER-8001", "https://legacy/api/orders/ORDER-8002"],   // stable sample records, for monitoring
      "lookup": { "url": "https://legacy/api/orders/{order_id}" } } } } }                             // one record, for an agent's call
```

* **Field types:** `string` · `enum` · `datetime` · `date` · `integer` · `number` · `boolean`, each optionally `optional` (absent or null reads as `null`). ISO-8601 timestamps with an offset or fractional seconds are normalised to UTC `…Z`; US `MM/DD/YYYY` is recognised but day/month order is never guessed.
* **`mapping`** is optional. Without one, it's inferred from the live sample and **accepted only if every required field matched with confidence**; otherwise registration is refused (422) with the reason.
* **Two kinds of read.** `probes` (or `sql`, or `path`) are *sample* records the controller watches for drift. `lookup` is how an *agent's* request for one record reaches the upstream. A binding needs samples to be monitored.
* **`inputs`** is a deliberately small JSON-Schema subset (flat; string/integer/number/boolean; `required`, `enum`, `pattern`, length and range). Unknown arguments are rejected and strings are capped at 256 characters unless you say otherwise.

### Connector types (server side)

| `type` | Reads | Notes |
|---|---|---|
| `http-json` | any JSON-over-HTTP API | arguments are URL-encoded into `lookup.url`; a `Sunset` header becomes a retirement notice |
| `delimited-file` | CSV/TSV/pipe under a `root` | refuses `..`, absolute paths and symlinks that leave the root |
| `sqlite` | a SQLite file | opened read-only where supported; arguments are bound parameters |
| `remote` | **a service you run, in any language** | `POST /fetch` (and optionally `/versions`, `/mapping`) |
| `module` | anything else (Postgres, MySQL, Kafka…) | a module whose default export is `async (options) => connector` |

`remote` is the language-agnostic door: if your upstream is only reachable from a Python gateway or a mainframe adapter, expose `/fetch` returning the [connector contract's](../controller/README.md#the-connector-contract) `FetchResult` and nothing else needs to change.

## Operating it

* **Reviews.** `GET /v1/reviews` lists what needs a person; `POST /v1/reviews/{id}/approve|reject|choices`. Approval is re-validated against the upstream *right now* and refused (`GATE_CLOSED`, with the evidence) if the candidate would corrupt records or otherwise fails the checks, and refused until every open choice is made (`CHOICES_REQUIRED`). A review whose change has gone away closes itself.
* **Migrations.** `POST /v1/bindings/{id}/migrations/preview`, then `/migrations`, then `/adapters/{a}/promote` twice (tested → canary → primary; the second needs a clean canary batch). `NEEDS_MAPPING` (409) means a person has to confirm fields or status codes; pass them as `choices`.
* **Events.** `GET /v1/events` (SSE, admin) pushes `review.opened`, `adapter.created`, `batch.completed`, `binding.added`, `binding.removed`. Events are live notifications; REST is the source of truth.
* **Audit.** `GET /v1/audit` — every state change made through the API records **who** asked for it (the token's name, never the secret).
* **Metrics.** `GET /metrics` (admin, Prometheus text): calls by tool and outcome, call time, open reviews, per-binding health, drift absorbed/blocked, in-flight calls. **Probes:** `/healthz` (liveness), `/readyz`.
* **Durability.** State is written atomically to `stateFile` (mode 0600) within about a second of any change, and flushed on `SIGINT`/`SIGTERM`.

## Security model

Enforced, with tests:

* It **refuses to start** with no tokens (or anonymously on a non-loopback host), and rejects tokens under 16 characters.
* Tokens are compared by SHA-256 digest in constant time; roles are separated structurally (every admin route is proven unreachable by an agent token).
* Arguments are validated against the tool's schema before anything else happens, and each connector then **encodes** them (URLs), **binds** them (SQL) or **matches** them exactly (files) — they are never spliced into a request or a query.
* `Origin` must be allow-listed (`corsOrigins`) or absent; in anonymous mode the `Host` must be loopback (DNS-rebinding defence). Bodies must be `application/json` and under `bodyLimitBytes`.
* The state file is owner-only; the file connector is confined to its root; admin errors never include stack traces.

Not provided: **TLS** (terminate it at a reverse proxy; do not expose this port directly), token rotation without a restart, per-token rate limits, multi-tenancy, OAuth for MCP clients (a static bearer token is used), redaction of upstream data in `GET /v1/state/snapshot`.

## What "scalable" does and doesn't mean here

* **One process owns the state** (single writer). Runtime calls run concurrently up to `maxConcurrentCalls` and shed load with `429` beyond it; state-changing operations are serialized. A call costs one upstream request plus in-memory detection, so throughput is bounded by the upstream, not the server. Nothing is cached — every call is fresh, deliberately.
* **Two instances are two separate controllers** with separate reviews and state, not a cluster. To spread load, partition *bindings* across instances (a binding lives on exactly one). A shared store and leader election are not built.
* Batches check bindings **sequentially**.

## Tests

```bash
cd server && npm install        # only for the official-MCP-client interop test (a dev dependency)
npm test                        # 26 tests, real sockets, real upstream; the SDK test skips if not installed
```

`tests/server.test.mjs` covers startup safety, roles, tool schemas, calls, injection attempts, the full drift and migration lifecycles over HTTP, SSE, persistence across a restart, body/Origin hardening, the concurrency cap, metrics, the scheduler, MCP, and graceful shutdown. `tests/mcp-sdk.test.mjs` runs the **official MCP SDK client** against it. `examples/att-sandbox/demo.sh` runs the end-to-end scenario against the MCP sandbox's real mock backend (33 checks plus 6 through the sandbox's own agent pipeline).

## Limits

Everything in the [controller's known limits](../controller/README.md#known-limits) applies — notably that regression checking assumes sampled records are stable between runs (point `probes` at stable records), and that an optional field that renames beyond recognition reads as `null` (the regression check still flags the lost values). Also: tools are read-only; there is no write path.
