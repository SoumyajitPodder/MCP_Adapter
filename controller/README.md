# lifecycle-controller

Watches an upstream (a REST API, a file feed, a database table — anything a connector can read), absorbs the changes that are safe, routes the ambiguous ones to a person with a recommendation and a confidence breakdown, and walks a breaking change through a tested → canary → primary migration with automatic rollback. It fails closed rather than guess.

It has **no dependencies**, touches **no DOM**, and holds **no module-level state**: everything lives inside a `createController()` instance. It knows nothing about any particular upstream — it only talks to *connectors*.

```js
import { createController, createHttpJsonConnector } from './controller/index.js';

const ctl = createController({
  connectors: { http: createHttpJsonConnector({ headers: { Authorization: `Bearer ${token}` } }) }
});

await ctl.addBinding({
  id: 'orders',
  connector: 'http',
  source: { versions: { v2: { probes: ['https://api.example.com/v2/orders/1001', 'https://api.example.com/v2/orders/1002'] } } },
  contract: { version: '1.0.0', fields: [
    { name: 'order_id', type: 'string' },
    { name: 'status', type: 'enum', values: ['in_progress', 'completed', 'cancelled'] },
    { name: 'created_at', type: 'datetime' } ] }
  // mapping: ...   optional — if omitted it is inferred, and only accepted if every field matched with confidence
});

await ctl.runBatch();                 // run on your own schedule: cron, a queue, setInterval
ctl.state.reviews                     // what needs a person right now
await ctl.liveCall('orders')          // canonical output, or a structured error — it never guesses
```

## The connector contract

A connector is a plain object. This is the **only** thing the controller knows about an upstream.

```js
{
  async fetch(binding, version, { limit, hints, args }) -> FetchResult, // required
  async listVersions(binding)                      -> string[],      // optional
  async operatorMapping(binding, version)          -> mapping | null // optional
}
```

| `FetchResult` field | Meaning |
|---|---|
| `status` | `200` ok · `404` not found · `410` gone/retired · anything else = unavailable |
| `records` | array of **flat** objects (nested JSON flattened to dot paths — `helpers.toRecords` does this) |
| `columns` | ordered column names for tabular sources (CSV, SQL); `null`/omitted for documents |
| `retirement` | `{ at: <ms timestamp> }` when this version is announced to retire (e.g. from a `Sunset` header) |
| `format` | parse settings actually used (e.g. `{ delimiter }`); echoed back as `hints` next time, so a format change is *detected*, not silently followed |
| `unreadable` | `true` if the body could not be parsed at all |
| `error` | human-readable reason when `status` is not 200 |

* `args` is present when an agent asked for **one specific record** (`liveCall(id, { args })`). A connector must use it safely — encode it, bind it as a parameter, never splice it into a URL or query — answer `404` if nothing matches and `400` if the arguments are unusable. Those two are *answers about the request*: they are returned as-is and never logged as drift or as an outage. With no `args`, `fetch` returns sample records for monitoring.
* `listVersions` is how the controller learns a new version exists. Omit it and the upstream is treated as one fixed version.
* `operatorMapping` is where a human- or config-supplied mapping can come from when automatic matching isn't confident enough. **Omit it if your upstream can't provide one** — the controller will then ask a person which upstream field holds each unresolved field, and what each unfamiliar status code means, instead of guessing.
* The controller wraps every call: a connector that throws, hangs (`options.fetchTimeoutMs`, default 15 s), or returns something half-formed becomes an ordinary "unavailable" result.

**Reference connectors** (all driver-agnostic; you inject the I/O):

| | Inject | `binding.source` |
|---|---|---|
| `createHttpJsonConnector({ fetch?, headers?, timeoutMs? })` | `fetch` (default: global) | `{ versions: { v2: { probes: [urls], recordsPath? } } }` — a `Sunset` header becomes `retirement` |
| `createDelimitedFileConnector({ readText })` | `readText(path)` — fs, S3, SFTP… | `{ versions: { v1: { path, delimiter? } } }` |
| `createSqlConnector({ query, placeholder? })` | `query(sql, values)` — pg, mysql2, sqlite… | `{ versions: { v1: { sql: 'SELECT * FROM t LIMIT {limit}', lookup: { sql: 'SELECT * FROM t WHERE id = :id', params: ['id'] } } } }` — use `SELECT *` so a renamed column surfaces as drift; `:id` becomes a **bound parameter** (`?`, or `$1` with `placeholder: 'dollar'`) |
| `createRemoteConnector({ url, headers?, capabilities? })` | a service **you run, in any language** | `POST {url}/fetch` (+ optional `/versions`, `/mapping`) returning the `FetchResult` above |

Writing your own is a dozen lines:

```js
ctl.registerConnector('kafka-snapshot', {
  async fetch(binding, version, { limit }) {
    const msgs = await readLatest(binding.source.topic, limit);          // your I/O
    return { status: 200, records: msgs.map(m => flatten(JSON.parse(m.value))) };
  }
});
```

## Controller API

`createController({ clock?, connectors?, options?: { fetchTimeoutMs, sampleSize } })`

| | |
|---|---|
| `addBinding(def, { quiet? })` / `removeBinding(id)` | `def`: `{ id, connector, contract, source?, mapping?, version?, displayName?, kind?, system?, iface? }` → `{ ok, binding }` or `{ ok:false, reason }` |
| `runBatch()` | the proactive run: every live adapter through the six-stage pipeline |
| `liveCall(id, { args? })` | the runtime path: same detection, **fails closed**. With `args`, serves the one record asked for; an unfamiliar change also opens a review immediately |
| `beginMigration(id, version, { choices? })` | one action: automatic mapping if confident, else the connector's `operatorMapping`, else `{ ok:false, reason:'needs_mapping' }`. `choices` = `{ fields: { target: sourcePath }, values: { 'target:upstreamValue': canonical } }` |
| `previewMigration(id, version, choices)` | read-only: what would a migration look like with these picks, and what's still open |
| `promote(id, adapterId)` · `retire(id, adapterId)` | tested → canary → primary; deprecated → retired |
| `approve(reviewId)` · `reject(reviewId)` · `setReviewChoice(reviewId, i, value)` | mapping reviews; approval re-validates against the upstream *now* |
| `deprecateContract(id)` | starts a contract's sunset window |
| `health(id)` · `state` · `binding(id)` · `primary(b)` · `untargeted(b)` | read-only queries (`state` is plain JSON) |
| `snapshot()` · `restore(json)` · `reset()` | persistence is "store this string" |
| `on(event, fn)` | `review.opened` · `adapter.created` · `binding.added` · `binding.removed` · `batch.completed` · `reset` · `restored` |
| `hooks.afterTranslate(fn)` | inspect or alter translated output before validation (used by tests to inject faults) |
| `registerConnector(name, impl)` · `audit(...)` | |

All state-changing calls are **serialized** through one queue, so two overlapping operations can't interleave at an `await`. `liveCall`, `previewMigration` and the queries are not queued.

The `clock` is `{ now(): ms, day(): int, format(ms): string }` — real time by default, injectable for tests or simulation. Retirement notices and contract sunsets are timestamps on that clock.

## Guarantees

* **Never guesses.** A rename is proposed with a confidence score and a human approves it; an unfamiliar status code or an ambiguous field is *asked about*, not assigned.
* **An outage is not drift.** A 503, a timeout, or a locked database is reported and re-checked — it never opens a migration review, is never counted as a blocked change, and never rolls back a healthy canary.
* **Fails closed.** Anything it can't translate into the canonical contract is an error to the caller, not a best-effort answer.
* **The old version is untouched** until the new one has passed the sandbox, a canary run, and shadow comparison against it.
* **Honest dead ends.** A breaking change with no newer version to move to is reported as exactly that.

## Known limits

* **Regression checking assumes sampled records are stable between runs.** Candidate output is compared against last-known-good *by key*. If a sampled record's values legitimately change (an order's status moving on), that reads as a regression. Point `probes` / queries at records that are stable, or fixtures.
* **State is in memory.** Use `snapshot()` / `restore()` to persist it; there is no built-in store.
* **Reads only.** No write-path tools, idempotency, or credentials management — connectors bring their own auth.
* **A lookup needs samples.** `lookup` serves an agent's request; `probes`/`sql`/`path` give the controller stable records to monitor. Without samples a binding can't be baselined.
* **Contract field types**: `string`, `enum`, `datetime`, `date`, `integer`, `number`, `boolean`, each optionally `optional`. Arrays and nested objects in the *canonical* contract aren't modelled (upstream JSON may be nested; it is flattened). A number that turns into a string is blocked, not coerced.
* **An optional field that vanishes or is renamed beyond recognition reads as `null`.** It is audited as absorbed, and the regression check against last-known-good fails the batch ("was TRK1, now null"), but live calls keep working. A rename the name metric can't see (`quantity_available` → `qty_avail`) *is* recognised when the new field holds the values the old one held for known records.
* **CSV**: quoted fields work; a quoted field spanning several lines does not. Values stay strings.
* **One process.** Batches run bindings sequentially.
* **JavaScript, with an HTTP/MCP front.** For other stacks use [`../server`](../server/README.md) (REST, SSE, MCP, Python and JS clients), or the `remote` connector to bring *your* upstream in from any language.

## Tests

```bash
node --no-warnings --test controller/tests/*.test.mjs
```

54 tests, no dependencies: the full lifecycle against an in-memory connector, a **real HTTP server**, a **real SQLite database** (skipped on Node < 22.5), delimited files, and a remote service; the richer contract types; parameterized calls with injection attempts against the HTTP and SQL connectors — plus a test that fails if anything in this folder imports a DOM global, a simulator, or anything outside the package.
