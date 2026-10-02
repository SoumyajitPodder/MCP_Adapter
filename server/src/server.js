// The HTTP front for a lifecycle controller.
//
//   agents   GET  /v1/tools · POST /v1/tools/{name}/call · POST /mcp      (role: agent)
//   people   bindings, reviews, migrations, audit, events, metrics         (role: admin)
//   anyone   /healthz · /readyz · /openapi.json
//
// Separation of duties is structural: nothing an agent token can reach can
// approve a review, start a migration, or change a binding.
//
// The controller import below is the ONE place this package touches it; to
// consume it as an installed package instead, change that single line.

import http from 'node:http';
import { createController } from '../../controller/index.js';
import { createAuth, isLoopback } from './auth.js';
import { HttpError, send, readJson } from './http.js';
import { createMetrics } from './metrics.js';
import { createFileStore, autosave } from './persist.js';
import { toolDefinition, validateArgs } from './schema.js';
import { handleMcpMessage, rpcError } from './mcp.js';
import { buildOpenApi } from './openapi.js';

export const VERSION = '0.1.0';
const STREAMED = Symbol('streamed');
const CALL_STATUS = { NOT_FOUND: 404, BAD_REQUEST: 400, CONTRACT_SUNSET: 410, DRIFT_BLOCKED: 502, UPSTREAM_UNAVAILABLE: 503, NO_SUCH_BINDING: 404, NO_PRIMARY: 503 };
const OBJ = { type: 'object' };
const REVIEW_OMIT = ['candidate'];

export async function createLifecycleServer(opts = {}) {
  const o = {
    host: '127.0.0.1', port: 0, bodyLimitBytes: 1 << 20, maxConcurrentCalls: 64, batchIntervalSeconds: 0, corsOrigins: [],
    heartbeatSeconds: 20, autosaveMs: 1000, retryBindingsSeconds: 30, log: () => {}, ...opts
  };
  const auth = createAuth({ tokens: o.tokens, allowAnonymous: o.allowAnonymous, host: o.host });
  const controller = o.controller || createController({ connectors: o.connectors, clock: o.clock, options: o.controllerOptions });
  if (o.controller && o.connectors) for (const [n, c] of Object.entries(o.connectors)) controller.registerConnector(n, c);

  /* ---------------- durable state ---------------- */
  const store = o.store || (o.stateFile ? createFileStore(o.stateFile) : null);
  if (store) { const text = store.load(); if (text) await controller.restore(text); }
  const saver = store ? autosave({ controller, store, intervalMs: o.autosaveMs, onError: e => o.log('error', 'state save failed: ' + e.message) }) : null;
  if (saver) saver.markClean();

  /* ---------------- configured bindings are seeds ----------------
     State wins: a binding already in the restored state keeps everything it
     has learned. One whose upstream isn't reachable yet is retried, so a
     slow start doesn't require a restart. */
  const pending = new Map();
  async function trySeed(def) {
    if (controller.binding(def.id)) { pending.delete(def.id); return true; }
    const r = await controller.addBinding(def, { quiet: true });
    if (r.ok) { pending.delete(def.id); if (saver) saver.touch(); return true; }
    if (!pending.has(def.id) || pending.get(def.id).reason !== r.reason) o.log('warn', `binding "${def.id}" not registered yet: ${r.reason}`);
    pending.set(def.id, { def, reason: r.reason });
    return false;
  }
  for (const def of o.bindings || []) await trySeed(def);

  /* ---------------- events ---------------- */
  const clients = new Set();
  const emitSse = (type, data) => {
    const msg = `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const r of clients) { try { r.write(msg); } catch { clients.delete(r); } }
  };
  controller.on('review.opened', ({ review, binding }) => emitSse('review.opened', review.kind === 'migration'
    ? { id: review.id, kind: 'migration', bindingId: binding.id, reason: review.reasonText, targetVersion: review.targetVersion }
    : { id: review.id, kind: 'mapping', bindingId: binding.id, events: review.events }));
  controller.on('adapter.created', ({ binding, adapter }) => emitSse('adapter.created', { bindingId: binding.id, adapterId: adapter.id, version: adapter.upstreamVersion, state: adapter.state }));
  controller.on('batch.completed', entry => emitSse('batch.completed', entry));
  controller.on('binding.added', ({ binding }) => emitSse('binding.added', { id: binding.id }));
  controller.on('binding.removed', ({ id }) => emitSse('binding.removed', { id }));

  /* ---------------- tool calls ---------------- */
  let inflight = 0;
  const metrics = createMetrics(controller, () => inflight);
  const findTool = name => controller.state.bindings.find(b => b.tool === name || b.id === name);
  const listTools = () => controller.state.bindings.filter(b => b.contract.state !== 'SUNSET').map(toolDefinition);

  // The one path every tool call takes, whether it arrived as REST or as MCP.
  async function runTool(name, rawArgs) {
    const b = findTool(name);
    if (!b) return { status: 404, body: { error: { code: 'NO_SUCH_TOOL', message: `no tool "${name}"` } } };
    if (b.contract.state === 'SUNSET') return { status: 410, body: { error: { code: 'CONTRACT_SUNSET', message: `${b.tool}@${b.contract.version} is sunset and no longer served` } } };
    const v = validateArgs(b.inputs, rawArgs);
    if (!v.ok) { metrics.observeCall(b.tool, 'bad_request', 0); return { status: 400, body: { error: { code: 'BAD_REQUEST', message: v.errors.join('; '), details: v.errors } } }; }
    if (inflight >= o.maxConcurrentCalls) { metrics.observeCall(b.tool, 'rate_limited', 0); return { status: 429, headers: { 'retry-after': '1' }, body: { error: { code: 'RATE_LIMITED', message: 'too many concurrent calls; retry shortly' } } }; }
    inflight++;
    const t0 = process.hrtime.bigint();
    try {
      const out = await controller.liveCall(b.id, { args: v.args });
      const secs = Number(process.hrtime.bigint() - t0) / 1e9;
      if (out.error) {
        const status = CALL_STATUS[out.error.code] || 500;
        metrics.observeCall(b.tool, String(out.error.code).toLowerCase(), secs);
        return { status, headers: status === 503 ? { 'retry-after': '30' } : {}, body: out };
      }
      metrics.observeCall(b.tool, 'ok', secs);
      return { status: 200, body: out };
    } finally { inflight--; }
  }

  /* ---------------- views (what leaves the building) ---------------- */
  const runView = r => r && { at: r.at, day: r.day, readiness: r.readiness, overall: r.overall, stages: r.stages.map(s => ({ name: s.name, status: s.status, lines: s.lines })) };
  const bindingView = (b, detailed = false) => ({
    id: b.id, tool: b.tool, displayName: b.displayName, description: b.description, kind: b.kind, system: b.system, iface: b.iface,
    connector: b.connector, source: b.source, health: controller.health(b), inputs: b.inputs,
    contract: { version: b.contract.version, state: b.contract.state, sunsetAt: b.contract.sunsetAt, fields: b.contract.fields },
    knownVersions: b.knownVersions,
    adapters: b.adapters.map(a => ({ id: a.id.slice(b.id.length + 1), version: a.upstreamVersion, state: a.state, note: a.note, mappingVersion: a.mapping.version, canaryPass: a.canaryPass,
      lastRun: a.lastRun ? (detailed ? runView(a.lastRun) : { at: a.lastRun.at, day: a.lastRun.day, readiness: a.lastRun.readiness, overall: a.lastRun.overall }) : null })),
    openReviews: controller.state.reviews.filter(r => r.bindingId === b.id && r.status === 'open').length
  });
  const reviewView = r => {
    const out = { ...r }; REVIEW_OMIT.forEach(k => delete out[k]);
    if (out.preview) { const { mapping, ...p } = out.preview; out.preview = p; }
    return out;
  };
  const bindingOr404 = id => { const b = controller.binding(id); if (!b) throw new HttpError(404, 'NOT_FOUND', `no binding "${id}"`); return b; };
  const reviewOr404 = id => { const r = controller.state.reviews.find(x => x.id === id); if (!r) throw new HttpError(404, 'NOT_FOUND', `no review "${id}"`); return r; };
  // Views expose the short id ("a2"), which is unique within a binding and safe to put in a URL;
  // the full internal id ("order.get/a2") is accepted too.
  const adapterOr404 = (b, id) => { const a = b.adapters.find(x => x.id === id || x.id === b.id + '/' + id); if (!a) throw new HttpError(404, 'NOT_FOUND', `no adapter "${id}" on ${b.id}`); return a; };
  const touch = () => { if (saver) saver.touch(); };

  /* ---------------- routes ---------------- */
  const routes = [];
  const route = (method, path, role, meta, handler, extra = {}) => routes.push({
    method, path, role, meta, handler, ...extra,
    re: new RegExp('^' + path.replace(/\{(\w+)\}/g, '([^/]+)') + '$'), names: [...path.matchAll(/\{(\w+)\}/g)].map(m => m[1])
  });
  const choicesSchema = { $ref: '#/components/schemas/MigrationChoices' };

  route('GET', '/healthz', 'public', { id: 'healthz', tag: 'ops', summary: 'Liveness' }, () => ({ body: { ok: true, version: VERSION } }));
  let ready = false;
  route('GET', '/readyz', 'public', { id: 'readyz', tag: 'ops', summary: 'Readiness: true once started and configured bindings have been attempted', errors: { 503: 'Not ready yet' } },
    () => ({ status: ready ? 200 : 503, body: { ready, bindings: controller.state.bindings.length, pendingBindings: [...pending].map(([id, p]) => ({ id, reason: p.reason })) } }));
  route('GET', '/openapi.json', 'public', { id: 'openapi', tag: 'ops', summary: 'This API, as OpenAPI 3' }, () => ({ body: buildOpenApi({ routes, version: VERSION }) }));
  route('GET', '/metrics', 'admin', { id: 'metrics', tag: 'ops', summary: 'Prometheus metrics', ok: { status: 200, type: 'text/plain', schema: { type: 'string' } } },
    () => ({ raw: metrics.render(), headers: { 'content-type': 'text/plain; version=0.0.4; charset=utf-8' } }));

  // ---- agents ----
  route('GET', '/v1/tools', 'agent', { id: 'listTools', tag: 'tools', summary: 'Tool definitions (MCP-style): the canonical contract is each tool\'s outputSchema', ok: { status: 200, schema: { type: 'object', properties: { tools: { type: 'array', items: { $ref: '#/components/schemas/ToolDefinition' } } } } } },
    () => ({ body: { tools: listTools() } }));
  route('GET', '/v1/tools/{name}', 'agent', { id: 'getTool', tag: 'tools', summary: 'One tool definition', ok: { status: 200, schema: { $ref: '#/components/schemas/ToolDefinition' } }, errors: { 404: 'No such tool' } },
    ({ params }) => { const t = listTools().find(x => x.name === params.name); if (!t) throw new HttpError(404, 'NO_SUCH_TOOL', `no tool "${params.name}"`); return { body: t }; });
  route('POST', '/v1/tools/{name}/call', 'agent', {
    id: 'callTool', tag: 'tools', summary: 'Call a tool. Returns the record(s) in the stable contract, or fails closed with an explanation',
    body: { $ref: '#/components/schemas/CallRequest' }, ok: { status: 200, schema: { $ref: '#/components/schemas/CallResult' } },
    errors: { 404: 'No such tool, or no record for those arguments (NOT_FOUND)', 410: 'Contract sunset', 429: 'Too many concurrent calls', 502: 'DRIFT_BLOCKED: the upstream changed in a way that cannot be absorbed safely', 503: 'Upstream unavailable' }
  }, async ({ params, body }) => { const r = await runTool(params.name, body && body.args); return { status: r.status, body: r.body, headers: r.headers }; });

  route('POST', '/mcp', 'agent', { id: 'mcp', tag: 'mcp', summary: 'MCP over Streamable HTTP (JSON-RPC 2.0, stateless): initialize, ping, tools/list, tools/call', body: OBJ, ok: { status: 200, description: 'JSON-RPC response (202 with no body for notifications)', schema: OBJ } }, async ({ req, res }) => {
    let msg;
    try { msg = await readJson(req, o.bodyLimitBytes); }
    catch (e) { if (e.code === 'BAD_JSON') return { status: 400, body: rpcError(null, -32700, 'Parse error') }; throw e; }
    const deps = { listTools, callTool: runTool, serverInfo: { name: 'lifecycle-controller', version: VERSION } };
    const reply = Array.isArray(msg) ? (await Promise.all(msg.map(m => handleMcpMessage(m, deps)))).filter(Boolean) : await handleMcpMessage(msg, deps);
    if (reply === null || (Array.isArray(reply) && !reply.length)) { res.writeHead(202); res.end(); return STREAMED; }
    return { body: reply };
  }, { manualBody: true });
  for (const method of ['GET', 'DELETE']) {
    route(method, '/mcp', 'agent', { id: 'mcp' + method, tag: 'mcp', summary: 'Not supported: this server is stateless and offers no server-initiated stream', errors: { 405: 'Use POST' } },
      () => ({ status: 405, headers: { allow: 'POST' }, body: { error: { code: 'METHOD_NOT_ALLOWED', message: 'use POST' } } }));
  }

  // ---- people ----
  route('GET', '/v1/bindings', 'admin', { id: 'listBindings', tag: 'bindings', summary: 'Every binding with its health and adapters' },
    () => ({ body: { bindings: controller.state.bindings.map(b => bindingView(b)) } }));
  route('POST', '/v1/bindings', 'admin', { id: 'addBinding', tag: 'bindings', summary: 'Register an API. Nothing is registered on a guess: with no mapping, one is inferred only if every field matches with confidence', body: { $ref: '#/components/schemas/BindingDefinition' }, bodyRequired: true, ok: { status: 201, description: 'Registered' }, errors: { 422: 'Could not register (reason in the message)' } },
    async ({ body }) => {
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new HttpError(400, 'BAD_REQUEST', 'body must be a binding definition');
      const r = await controller.addBinding(body);
      if (!r.ok) throw new HttpError(422, 'INVALID_BINDING', r.reason, r.unresolved);
      touch(); return { status: 201, body: bindingView(r.binding, true), binding: r.binding };
    });
  route('GET', '/v1/bindings/{id}', 'admin', { id: 'getBinding', tag: 'bindings', summary: 'One binding, including its latest pipeline run', errors: { 404: 'No such binding' } },
    ({ params }) => ({ body: bindingView(bindingOr404(params.id), true) }));
  route('DELETE', '/v1/bindings/{id}', 'admin', { id: 'removeBinding', tag: 'bindings', summary: 'Remove a binding', errors: { 404: 'No such binding' } },
    async ({ params }) => { bindingOr404(params.id); await controller.removeBinding(params.id); touch(); return { body: { ok: true } }; });
  route('POST', '/v1/bindings/{id}/contract/deprecate', 'admin', { id: 'deprecateContract', tag: 'bindings', summary: 'Start the contract\'s sunset window', errors: { 409: 'Not active' } },
    async ({ params }) => { const b = bindingOr404(params.id); const r = await controller.deprecateContract(b.id); if (!r.ok) throw new HttpError(409, 'NOT_ACTIVE', 'the contract is not active'); return { body: bindingView(b), binding: b }; });

  route('POST', '/v1/batch', 'admin', { id: 'runBatch', tag: 'operations', summary: 'Run the proactive check against every binding now' },
    async () => ({ body: await controller.runBatch() }));

  route('GET', '/v1/reviews', 'admin', { id: 'listReviews', tag: 'reviews', summary: 'Reviews: mapping reviews and migration reviews', query: [{ name: 'status', description: 'open (default), all, approved, rejected, resolved, superseded' }, { name: 'binding', description: 'filter by binding id' }] },
    ({ url }) => {
      const status = url.searchParams.get('status') || 'open', binding = url.searchParams.get('binding');
      return { body: { reviews: controller.state.reviews.filter(r => (status === 'all' || r.status === status) && (!binding || r.bindingId === binding)).map(reviewView) } };
    });
  route('GET', '/v1/reviews/{id}', 'admin', { id: 'getReview', tag: 'reviews', summary: 'One review', errors: { 404: 'No such review' } }, ({ params }) => ({ body: reviewView(reviewOr404(params.id)) }));
  route('POST', '/v1/reviews/{id}/approve', 'admin', { id: 'approveReview', tag: 'reviews', summary: 'Approve a mapping review. Re-validated against the upstream right now; refused if any choice is open or the checks fail', errors: { 409: 'NOT_OPEN, CHOICES_REQUIRED, GATE_CLOSED or NOT_APPROVABLE' } },
    async ({ params }) => {
      const rv = reviewOr404(params.id);
      if (rv.kind === 'migration') throw new HttpError(409, 'NOT_APPROVABLE', 'migration reviews are driven through /migrations and adapter promotion');
      if (rv.status !== 'open') throw new HttpError(409, 'NOT_OPEN', `review ${rv.id} is ${rv.status}`);
      const open = rv.choices.filter(c => !c.selected);
      if (open.length) throw new HttpError(409, 'CHOICES_REQUIRED', 'every choice must be made first', open.map(c => c.key));
      const r = await controller.approve(rv.id);
      if (!r.ok) throw new HttpError(409, 'GATE_CLOSED', 'approval refused: the candidate mapping does not pass the sandbox and shadow checks against the upstream as it is now', { sandbox: rv.eval && rv.eval.sandbox, shadow: rv.eval && rv.eval.shadow });
      return { body: { ok: true, review: reviewView(rv) }, binding: controller.binding(rv.bindingId) };
    });
  route('POST', '/v1/reviews/{id}/reject', 'admin', { id: 'rejectReview', tag: 'reviews', summary: 'Reject a review', errors: { 409: 'Not open' } },
    async ({ params }) => {
      const rv = reviewOr404(params.id);
      const r = await controller.reject(rv.id);
      if (!r.ok) throw new HttpError(409, 'NOT_OPEN', `review ${rv.id} is ${rv.status}`);
      return { body: { ok: true, review: reviewView(rv) }, binding: controller.binding(rv.bindingId) };
    });
  route('POST', '/v1/reviews/{id}/choices', 'admin', { id: 'chooseReview', tag: 'reviews', summary: 'Record a reviewer\'s pick for one open choice; the sandbox is re-run with it', body: { type: 'object', properties: { index: { type: 'integer' }, value: { type: ['string', 'null'] } }, required: ['index', 'value'] }, bodyRequired: true, errors: { 409: 'Not open' } },
    async ({ params, body }) => {
      const rv = reviewOr404(params.id);
      if (rv.status !== 'open' || rv.kind === 'migration') throw new HttpError(409, 'NOT_OPEN', `review ${rv.id} has no open choices`);
      const ch = Number.isInteger(body.index) ? rv.choices[body.index] : null;
      if (!ch) throw new HttpError(400, 'BAD_REQUEST', `"index" must be 0..${rv.choices.length - 1}`);
      if (body.value !== null && !ch.options.includes(body.value)) throw new HttpError(400, 'BAD_REQUEST', `"value" must be null or one of: ${ch.options.join(', ')}`);
      await controller.setReviewChoice(rv.id, body.index, body.value); touch();
      return { body: reviewView(rv), binding: controller.binding(rv.bindingId) };
    });

  const checkChoices = c => {
    if (c === undefined) return undefined;
    const okMap = m => m === undefined || (m && typeof m === 'object' && !Array.isArray(m) && Object.values(m).every(v => typeof v === 'string'));
    if (!c || typeof c !== 'object' || !okMap(c.fields) || !okMap(c.values)) throw new HttpError(400, 'BAD_REQUEST', '"choices" must be { fields?: {target: path}, values?: {"target:value": canonical} }');
    return c;
  };
  const needVersion = (b, body) => {
    if (typeof body.version !== 'string' || !body.version) throw new HttpError(400, 'BAD_REQUEST', '"version" is required');
    if (!(b.knownVersions || []).includes(body.version)) throw new HttpError(422, 'UNKNOWN_VERSION', `${b.id} does not offer "${body.version}" (known: ${(b.knownVersions || []).join(', ') || 'none'}); run a batch to refresh`);
    return body.version;
  };
  route('POST', '/v1/bindings/{id}/migrations/preview', 'admin', { id: 'previewMigration', tag: 'migrations', summary: 'Read-only: what a migration would look like with these picks, and what is still open', body: { type: 'object', properties: { version: { type: 'string' }, choices: choicesSchema }, required: ['version'] }, bodyRequired: true, errors: { 422: 'Unknown version', 502: 'Upstream unavailable' } },
    async ({ params, body }) => {
      const b = bindingOr404(params.id), version = needVersion(b, body);
      const p = await controller.previewMigration(b.id, version, checkChoices(body.choices) || {});
      if (!p) throw new HttpError(502, 'UPSTREAM_UNAVAILABLE', `${version} could not be read`);
      const { mapping, ...view } = p; return { body: view };
    });
  route('POST', '/v1/bindings/{id}/migrations', 'admin', { id: 'beginMigration', tag: 'migrations', summary: 'Start a migration: automatic mapping if confident, else the connector\'s, else NEEDS_MAPPING (supply `choices`)', body: { type: 'object', properties: { version: { type: 'string' }, choices: choicesSchema }, required: ['version'] }, bodyRequired: true, ok: { status: 201, description: 'Candidate created' }, errors: { 409: 'NEEDS_MAPPING: a person has to confirm fields or values', 422: 'Unknown version', 502: 'Upstream unavailable' } },
    async ({ params, body }) => {
      const b = bindingOr404(params.id), version = needVersion(b, body);
      const r = await controller.beginMigration(b.id, version, { choices: checkChoices(body.choices) });
      if (r.ok) return { status: 201, body: { ok: true, usedConnectorMapping: !!r.fallback, binding: bindingView(b, true) }, binding: b };
      if (r.reason === 'needs_mapping') throw new HttpError(409, 'NEEDS_MAPPING', 'a person has to confirm which upstream field holds each unresolved field, and what unfamiliar values mean', r.unresolved);
      throw new HttpError(502, 'UPSTREAM_UNAVAILABLE', r.reason);
    });
  for (const [verb, fn] of [['promote', 'promote'], ['retire', 'retire']]) {
    route('POST', `/v1/bindings/{id}/adapters/{adapter}/${verb}`, 'admin', { id: verb + 'Adapter', tag: 'migrations', summary: verb === 'promote' ? 'Promote a candidate: tested → canary → primary (each step gated)' : 'Retire a deprecated adapter', errors: { 409: 'Not allowed from the adapter\'s current state' } },
      async ({ params }) => {
        const b = bindingOr404(params.id), a = adapterOr404(b, params.adapter);
        const r = await controller[fn](b.id, a.id);
        if (!r.ok) throw new HttpError(409, 'NOT_ALLOWED', `${a.id} is ${a.state}${verb === 'promote' ? ' and has not passed the checks needed to move on' : ''}`);
        return { body: bindingView(b, true), binding: b };
      });
  }

  route('GET', '/v1/audit', 'admin', { id: 'audit', tag: 'audit', summary: 'The audit trail, newest first', query: [{ name: 'limit', type: 'integer' }, { name: 'binding' }, { name: 'actor' }, { name: 'action' }, { name: 'afterSeq', type: 'integer' }] },
    ({ url }) => {
      const q = k => url.searchParams.get(k);
      const limit = Math.min(Math.max(parseInt(q('limit') || '100', 10) || 100, 1), 500), after = parseInt(q('afterSeq') || '0', 10) || 0;
      return { body: { entries: controller.state.log.filter(l => l.seq > after && (!q('binding') || l.binding === q('binding')) && (!q('actor') || l.actor === q('actor')) && (!q('action') || l.action === q('action'))).slice(0, limit) } };
    });
  route('GET', '/v1/state/snapshot', 'admin', { id: 'snapshot', tag: 'ops', summary: 'The whole controller state as JSON (what is persisted)' }, () => ({ body: JSON.parse(controller.snapshot()) }));
  route('GET', '/v1/events', 'admin', { id: 'events', tag: 'events', summary: 'Server-sent events: review.opened, adapter.created, batch.completed, binding.added, binding.removed', ok: { status: 200, type: 'text/event-stream', schema: { type: 'string' } } },
    ({ req, res }) => {
      res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache, no-transform', connection: 'keep-alive', 'x-accel-buffering': 'no' });
      res.write('retry: 3000\n: connected\n\n');
      clients.add(res); req.on('close', () => clients.delete(res));
      return STREAMED;
    });

  /* ---------------- request pipeline ---------------- */
  const allowedOrigin = origin => o.corsOrigins.includes('*') || o.corsOrigins.includes(origin);
  async function handle(req, res) {
    let routeLabel = 'unmatched', status = 500;
    try {
      res.setHeader('x-content-type-options', 'nosniff'); res.setHeader('cache-control', 'no-store');
      const url = new URL(req.url, 'http://placeholder');
      const origin = req.headers.origin;
      if (origin) {
        if (!allowedOrigin(origin)) throw new HttpError(403, 'FORBIDDEN_ORIGIN', 'this origin is not allowed');
        res.setHeader('access-control-allow-origin', origin); res.setHeader('vary', 'Origin'); res.setHeader('access-control-expose-headers', 'Retry-After');
      }
      if (auth.anonymous) { // no tokens means local development: refuse anything that isn't addressed to loopback (DNS rebinding)
        const host = String(req.headers.host || '').replace(/:\d+$/, '');
        if (!isLoopback(host)) throw new HttpError(403, 'FORBIDDEN_HOST', 'anonymous mode only answers to loopback hostnames');
      }
      if (req.method === 'OPTIONS') {
        res.writeHead(204, { 'access-control-allow-methods': 'GET, POST, DELETE, OPTIONS', 'access-control-allow-headers': 'authorization, content-type, mcp-protocol-version', 'access-control-max-age': '600' });
        res.end(); status = 204; return;
      }
      let path; try { path = url.pathname.length > 1 ? url.pathname.replace(/\/+$/, '') : url.pathname; } catch { throw new HttpError(400, 'BAD_REQUEST', 'bad path'); }
      const candidates = routes.filter(r => r.re.test(path));
      const hit = candidates.find(r => r.method === req.method);
      if (!hit) {
        if (candidates.length) throw Object.assign(new HttpError(405, 'METHOD_NOT_ALLOWED', 'method not allowed here'), { headers: { allow: candidates.map(c => c.method).join(', ') } });
        throw new HttpError(404, 'NOT_FOUND', 'no such route');
      }
      routeLabel = hit.path;
      let identity = { name: 'public', role: 'public' };
      if (hit.role !== 'public') {
        identity = auth.authenticate(req);
        if (!identity) throw Object.assign(new HttpError(401, 'UNAUTHORIZED', 'a valid bearer token is required'), { headers: { 'www-authenticate': 'Bearer' } });
        if (!auth.can(identity, hit.role)) throw new HttpError(403, 'FORBIDDEN', `this needs the ${hit.role} role`);
      }
      const m = hit.re.exec(path);
      const params = {}; try { hit.names.forEach((n, i) => { params[n] = decodeURIComponent(m[i + 1]); }); } catch { throw new HttpError(400, 'BAD_REQUEST', 'bad path encoding'); }
      const body = (['POST', 'PUT', 'PATCH'].includes(req.method) && !hit.manualBody) ? await readJson(req, o.bodyLimitBytes) : undefined;
      const out = await hit.handler({ req, res, url, params, body, identity });
      if (out === STREAMED) { status = res.statusCode; return; }
      status = out.status || 200;
      if (hit.role === 'admin' && req.method !== 'GET' && status < 300) { // every state change records WHO asked for it
        controller.audit(out.binding || null, 'SYSTEM', `${req.method} ${path} by ${identity.name}`, 'api', { actor: 'operator', action: 'API_REQUEST' });
      }
      if (out.raw !== undefined) { res.writeHead(status, { 'content-length': Buffer.byteLength(out.raw), ...(out.headers || {}) }); res.end(out.raw); }
      else send(res, status, out.body, out.headers);
    } catch (e) {
      if (e instanceof HttpError) {
        status = e.status;
        send(res, status, { error: { code: e.code, message: e.message, ...(e.extra !== undefined ? { details: e.extra } : {}) } }, { ...(e.headers || {}), ...(status === 413 ? { connection: 'close' } : {}) });
      } else {
        status = 500; o.log('error', `unhandled: ${e && e.stack || e}`);
        if (!res.headersSent) send(res, 500, { error: { code: 'INTERNAL', message: 'internal error' } });
        else res.end();
      }
    } finally { metrics.observeHttp(routeLabel, status); }
  }

  /* ---------------- lifecycle ---------------- */
  const server = http.createServer((req, res) => { handle(req, res); });
  let batching = false;
  const runScheduled = async () => {
    if (batching) return; batching = true;
    try { await controller.runBatch(); } catch (e) { o.log('error', 'scheduled batch failed: ' + e.message); } finally { batching = false; }
  };
  const timers = [];
  if (o.batchIntervalSeconds > 0) timers.push(setInterval(runScheduled, o.batchIntervalSeconds * 1000));
  timers.push(setInterval(() => { for (const { def } of [...pending.values()]) trySeed(def).catch(() => {}); }, o.retryBindingsSeconds * 1000));
  timers.push(setInterval(() => { for (const r of clients) { try { r.write(': keep-alive\n\n'); } catch { clients.delete(r); } } }, o.heartbeatSeconds * 1000));
  timers.forEach(t => t.unref());

  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(o.port, o.host, resolve); });
  const addr = server.address();
  // Ready means registered AND assessed: run the first batch now so no binding
  // reports PENDING, and a restart picks up whatever changed while it was down.
  if (o.runBatchOnStart !== false && controller.state.bindings.length) { batching = true; try { await controller.runBatch(); } catch (e) { o.log('error', 'initial batch failed: ' + e.message); } finally { batching = false; } }
  ready = true;

  let closed = false;
  async function close() {
    if (closed) return; closed = true;
    timers.forEach(clearInterval);
    for (const r of clients) { try { r.end(); } catch { /* already gone */ } }
    clients.clear();
    const done = new Promise(r => server.close(r));
    if (server.closeAllConnections) server.closeAllConnections();
    await done;
    if (saver) saver.stop();
  }
  return { controller, routes, server, port: addr.port, host: o.host, url: `http://${o.host === '::1' ? '[::1]' : o.host}:${addr.port}`, close, flush: () => saver && saver.flush() };
}
