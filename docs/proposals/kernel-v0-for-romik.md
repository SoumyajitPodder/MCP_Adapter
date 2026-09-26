# Shared kernel proposal v0: for Romik

**From:** Shaswat (§5–9) · **Status:** draft for discussion. Nothing is frozen until we both agree (brief §0.4).
**Code:** `packages/adapter-kernel/`. It's pure Pydantic and Protocols, and it has tests. **Rendered reference:** `docs/SPEC.md` §2–3.

## What I need from you

A yes, no, or counter-proposal on each item below. Items 1–5 change the brief's §3, so they're the important ones.

### 1. Connectors report delivery status (R-002)

Connectors return a `DeliveryStatus` on `ToolResult.delivery`: `not_sent`, `sent_no_response`, `acked`, or `rejected_no_effect`. It's internal and stripped before the result reaches an agent.
**Why:** idempotency can't tell "connection refused before send" from "timed out after send" by looking at an error code. Getting that wrong either duplicates a customer action or leaves one stuck.
**Your side:** each connector sets this field. That's roughly one line per exit path.

### 2. `retry_safe: bool` becomes `RetryPolicy` (R-003)

`never | immediate | after_delay`, with `AdapterError.retry_after_ms`. A boolean can't say "retry later" (`DUPLICATE_IN_PROGRESS`).

### 3. Two context types (R-008)

`RequestContext` (before auth: correlation ID, trace ID, tool, requested version, idempotency key) and `CallContext` (after auth: + `agent_id`, resolved `semantic_version`, `behavior`). A stage can never see a missing agent ID.

### 4. Version resolution before access control (R-006)

§9 checks the version range before your §4 lifecycle gate runs. It needs a pure `resolve_version(tool, requested_version) -> semantic_version` from your registry at the pipeline entry.
**Question:** does the version travel in the tool name (`order.get@1`) or as a separate field?

### 5. Mapping-aware upstream diffs (R-010)

Contract CI would read your `mapping_registry` so that an upstream field no mapping uses doesn't block a merge. **Question:** can the registry expose "which upstream paths does mapping M read"?

### 6. Contract-format additions (brief §3.6)

- `behavior: read_only | mutating | destructive` per tool. Idempotency depends on it.
- `x-sensitivity: public | internal | pii | secret` per schema field. Redaction is allow-list based, so **a missing annotation means masked**.

### 7. Correlation ID and idempotency key travel in MCP `params._meta`

Namespaced keys, set by the orchestrator. Not in tool arguments: an LLM regenerates arguments on retry, which would mint a new idempotency key each time.

### 8. Who owns the MCP server process?

Nothing in §1–9 owns the actual MCP server: the process that accepts connections and turns `tools/list` and `tools/call` into pipeline calls.

What my half needs from it:
- The correlation wrapper (§8) must wrap everything, so it sits at the server's entry.
- The `tools/list` filter (§9) runs inside the list handler.

My code is transport-neutral. The server hands it an `InboundCall(tool, arguments, meta)` and gets a `ToolResult` back, so this works whoever owns the server.

**Proposal:** a thin shared entry module in the kernel, reviewed by both of us, that wires the MCP SDK to that interface. Alternatives: you own it, or I own it. I'd like to decide this together.

### 9. Smaller confirmations

- Error code names (`adapter_kernel.errors.ErrorCode`). Your codes are `DRIFT_BLOCKED`, `UPSTREAM_UNAVAILABLE` and `CONTRACT_VIOLATION`.
- `ShapeModel` as a kernel type, shared with your file profiler and drift detector. The hash covers `source_id` + operations only.
- One classifier implementation shared by runtime drift and contract CI.
- Package name `adapter-kernel`, semver, living in its own workspace member until we pick monorepo vs separate repos.
- **Python 3.12** on both sides.
