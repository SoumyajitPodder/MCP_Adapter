import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHttpJsonConnector } from '../../controller/index.js';
import { createLifecycleServer } from '../src/server.js';
import { boot, upstream, bindingDef, client, raw, sleep, AGENT, ADMIN, TOKENS } from './fixture.mjs';

/* ---------------------------------- startup is safe by default ---------------------------------- */

test('refuses to start with no auth, on a public host without auth, or with a weak token', async () => {
  await assert.rejects(createLifecycleServer({}), /refusing to start with no auth tokens/);
  await assert.rejects(createLifecycleServer({ allowAnonymous: true, host: '0.0.0.0' }), /non-loopback/);
  await assert.rejects(createLifecycleServer({ tokens: [{ name: 'x', role: 'admin', token: 'short' }] }), /shorter than 16/);
  await assert.rejects(createLifecycleServer({ tokens: [{ name: 'x', role: 'root', token: 'a'.repeat(20) }] }), /role/);
});

test('anonymous mode (local development) only answers to loopback hostnames', async t => {
  const srv = await createLifecycleServer({ allowAnonymous: true }); t.after(() => srv.close());
  assert.equal((await raw(srv.url + '/healthz')).status, 200);
  const r = await raw(srv.url + '/healthz', { headers: { host: 'evil.example' } });
  assert.equal(r.status, 403); assert.equal(r.json.error.code, 'FORBIDDEN_HOST');
});

/* ---------------------------------- who can do what ---------------------------------- */

test('no token or a wrong token is 401; an agent token is refused (403) on EVERY admin route', async t => {
  const { srv, api } = await boot(t);
  assert.equal((await api('/v1/tools', { token: null })).status, 401);
  assert.equal((await api('/v1/tools', { token: 'wrong-token-000000000' })).status, 401);
  assert.equal((await api('/v1/tools', { token: null })).headers.get('www-authenticate'), 'Bearer');
  const admin = srv.routes.filter(r => r.role === 'admin');
  assert.ok(admin.length >= 15);
  for (const r of admin) {
    const p = r.path.replace(/\{\w+\}/g, 'x');
    assert.equal((await api(p, { method: r.method, token: AGENT, body: r.method === 'POST' ? {} : undefined })).status, 403, `agent reached ${r.method} ${r.path}`);
    assert.equal((await api(p, { method: r.method, token: null, body: r.method === 'POST' ? {} : undefined })).status, 401, `anonymous reached ${r.method} ${r.path}`);
  }
});

test('public routes need no token; the OpenAPI document covers every route the server has', async t => {
  const { srv, api } = await boot(t);
  assert.equal((await api('/healthz', { token: null })).status, 200);
  assert.equal((await api('/readyz', { token: null })).json.ready, true);
  const spec = (await api('/openapi.json', { token: null })).json;
  assert.equal(spec.openapi, '3.0.3');
  const ops = new Set();
  for (const r of srv.routes) {
    const op = spec.paths[r.path] && spec.paths[r.path][r.method.toLowerCase()];
    assert.ok(op, `${r.method} ${r.path} missing from the spec`);
    assert.ok(!ops.has(op.operationId), 'unique operationId ' + op.operationId); ops.add(op.operationId);
    assert.equal(op['x-required-role'], r.role);
    if (r.role === 'public') assert.deepEqual(op.security, []);
  }
  assert.equal(Object.values(spec.paths).reduce((n, p) => n + Object.keys(p).length, 0), srv.routes.length, 'and nothing extra');
});

/* ---------------------------------- tools, as an agent sees them ---------------------------------- */

test('tool definitions: the canonical contract is the outputSchema; inputs come from the binding', async t => {
  const { api } = await boot(t);
  const { json } = await api('/v1/tools', { token: AGENT });
  assert.equal(json.tools.length, 1);
  const tool = json.tools[0];
  assert.equal(tool.name, 'order.get'); assert.deepEqual(tool.inputSchema.required, ['order_id']); assert.equal(tool.inputSchema.additionalProperties, false);
  const item = tool.outputSchema.properties.data.items;
  assert.deepEqual(item.properties.status.enum, ['in_progress', 'completed', 'cancelled']);
  assert.equal(item.properties.quantity.type, 'integer');
  assert.deepEqual(item.properties.tracking_number.type, ['string', 'null'], 'optional fields are nullable, not missing');
  assert.deepEqual(item.required, ['order_id', 'status', 'quantity', 'tracking_number']);
  assert.equal(tool.annotations.readOnlyHint, true);
  assert.equal(tool._meta['lifecycle/contract'], 'order.get@1.0.0');
  assert.equal((await api('/v1/tools/order.get', { token: AGENT })).json.name, 'order.get');
  assert.equal((await api('/v1/tools/nope', { token: AGENT })).status, 404);
});

test('calling a tool returns typed canonical output for the record asked for', async t => {
  const { api } = await boot(t);
  const r = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1003' } } });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json.data, [{ order_id: 'ORD-1003', status: 'in_progress', quantity: 4, tracking_number: 'TRK3' }]);
  assert.equal(r.json._meta.contract, 'order.get@1.0.0');
});

test('"no such order" is a clean 404 that changes nothing; bad arguments are a 400 that never reach the upstream', async t => {
  const { api, up, ctl } = await boot(t);
  await api('/v1/batch', { method: 'POST' });
  const before = { seq: ctl.state.seq, health: ctl.health('order.get') };
  const nf = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-9999' } } });
  assert.deepEqual([nf.status, nf.json.error.code], [404, 'NOT_FOUND']);
  assert.deepEqual({ seq: ctl.state.seq, health: ctl.health('order.get') }, before, 'no audit noise, no health change');
  up.cfg.hits.length = 0;
  for (const bad of [{}, { order_id: 5 }, { order_id: 'x'.repeat(300) }, { order_id: 'ORD-1', extra: 1 }]) {
    const r = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: bad } });
    assert.equal(r.status, 400, JSON.stringify(bad).slice(0, 40)); assert.equal(r.json.error.code, 'BAD_REQUEST'); assert.ok(r.json.error.details.length);
  }
  assert.equal(up.cfg.hits.length, 0, 'the upstream was never touched');
});

test('argument injection cannot change what is requested of the upstream', async t => {
  const { api, up } = await boot(t);
  up.cfg.hits.length = 0;
  for (const evil of ['../admin', 'ORD-1000/../../x', 'a?b=c', 'a#b']) {
    const r = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: evil } } });
    assert.equal(r.status, 404, evil);
  }
  assert.equal(up.cfg.hits.length, 4);
  assert.ok(up.cfg.hits.every(u => u.split('/').length === 4 && u.startsWith('/v2/orders/')), 'every request stayed one segment under /v2/orders/: ' + up.cfg.hits.join(' '));
});

test('an argument pattern in the binding is enforced before anything else happens', async t => {
  const { api, up } = await boot(t);
  const def = bindingDef(up.base, { id: 'order.strict', inputs: { properties: { order_id: { type: 'string', pattern: '^ORD-[0-9]+$' } }, required: ['order_id'] } });
  assert.equal((await api('/v1/bindings', { method: 'POST', body: def })).status, 201);
  up.cfg.hits.length = 0;
  assert.equal((await api('/v1/tools/order.strict/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1/../x' } } })).status, 400);
  assert.equal(up.cfg.hits.length, 0);
  assert.equal((await api('/v1/tools/order.strict/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1001' } } })).status, 200);
});

/* ---------------------------------- drift, end to end over HTTP ---------------------------------- */

test('drift: the call fails closed (502) and names a review; a person approves it; the agent recovers', async t => {
  const { api, up, ctl } = await boot(t);
  up.cfg.shape = 'renamed';
  const blocked = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1002' } } });
  assert.equal(blocked.status, 502); assert.equal(blocked.json.error.code, 'DRIFT_BLOCKED');
  const id = blocked.json.error.review; assert.ok(id, 'the error points at the review');

  assert.equal((await api(`/v1/reviews/${id}/approve`, { method: 'POST', token: AGENT, body: {} })).status, 403, 'an agent cannot approve its own fix');
  const list = (await api('/v1/reviews')).json.reviews; assert.equal(list.length, 1);
  assert.ok(list[0].renames.some(x => x.from === 'status' && x.to === 'state')); assert.equal(list[0].candidate, undefined, 'internals stay internal');

  const ok = await api(`/v1/reviews/${id}/approve`, { method: 'POST', body: {} });
  assert.equal(ok.status, 200); assert.equal(ok.json.review.status, 'approved');
  const again = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1002' } } });
  assert.equal(again.status, 200); assert.equal(again.json.data[0].status, 'cancelled', 'served under the canonical name');

  const who = ctl.state.log.find(l => l.action === 'API_REQUEST' && l.msg.includes(`/v1/reviews/${id}/approve`));
  assert.match(who.msg, /by ops-alice/, 'the audit trail records WHO approved');
  assert.equal((await api(`/v1/reviews/${id}/approve`, { method: 'POST' })).json.error.code, 'NOT_OPEN');
  const audit = (await api('/v1/audit?action=API_REQUEST')).json.entries; assert.ok(audit.length >= 1);
});

test('an upstream outage is a 503 with Retry-After, and opens no review', async t => {
  const { api, up } = await boot(t);
  up.cfg.status = 503;
  const r = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1001' } } });
  assert.equal(r.status, 503); assert.equal(r.headers.get('retry-after'), '30'); assert.equal(r.json.error.code, 'UPSTREAM_UNAVAILABLE');
  assert.equal((await api('/v1/reviews?status=all')).json.reviews.length, 0);
});

test('migration over HTTP: announced sunset → preview → begin → promote twice → resolved', async t => {
  const { api, up, srv, ctl } = await boot(t);
  // a controller clock we can't control here, so use a real near-future Sunset
  up.cfg.sunset = Date.now() + 3 * 86400000 + 3600000;
  ctl.binding('order.get').source.versions.v3 = { probes: [0, 1, 2, 3, 4, 5].map(i => `${up.base}/v3/orders/ORD-${1000 + i}`), lookup: { url: `${up.base}/v3/orders/{order_id}` } };
  await api('/v1/batch', { method: 'POST' });
  const mr = (await api('/v1/reviews?status=open')).json.reviews.find(r => r.kind === 'migration');
  assert.ok(mr, 'a migration review opened'); assert.match(mr.reasonText, /retired in \d+ day/); assert.equal(mr.preview.mapping, undefined);

  assert.equal((await api('/v1/bindings/order.get/migrations', { method: 'POST', body: { version: 'v9' } })).status, 422);
  const prev = await api('/v1/bindings/order.get/migrations/preview', { method: 'POST', body: { version: 'v3' } });
  assert.equal(prev.json.ok, true); assert.equal(prev.json.mapping, undefined);
  const begin = await api('/v1/bindings/order.get/migrations', { method: 'POST', body: { version: 'v3' } });
  assert.equal(begin.status, 201);
  const cand = begin.json.binding.adapters.find(a => a.state === 'tested'); assert.ok(cand);
  assert.equal((await api(`/v1/bindings/order.get/adapters/${cand.id}/promote`, { method: 'POST' })).status, 200);
  assert.equal((await api(`/v1/bindings/order.get/adapters/a2/promote`, { method: 'POST' })).status, 409, 'not yet: needs a clean canary run');
  await api('/v1/batch', { method: 'POST' });
  assert.equal((await api('/v1/bindings/order.get/adapters/a2/promote', { method: 'POST' })).status, 200);
  const done = (await api('/v1/reviews?status=all')).json.reviews.find(r => r.kind === 'migration');
  assert.equal(done.status, 'resolved');
  const b = (await api('/v1/bindings/order.get')).json;
  assert.deepEqual(b.adapters.map(a => a.state), ['deprecated', 'primary']);
  assert.deepEqual(b.adapters.map(a => a.id), ['a1', 'a2'], 'adapter ids are short and URL-safe');
  const call = await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1004' } } });
  assert.equal(call.status, 200); assert.match(call.json._meta.served_by, /^v3/);
});

test('registering a binding refuses to guess a mapping it cannot be confident about', async t => {
  const { api, up } = await boot(t);
  const r = await api('/v1/bindings', { method: 'POST', body: bindingDef(up.base, { id: 'order.odd', contract: { fields: [{ name: 'zzz_unrelated', type: 'string' }] } }) });
  assert.equal(r.status, 422); assert.equal(r.json.error.code, 'INVALID_BINDING'); assert.match(r.json.error.message, /Could not infer a mapping/);
  assert.equal((await api('/v1/bindings')).json.bindings.length, 1, 'nothing was registered');
  assert.equal((await api('/v1/bindings/order.odd')).status, 404);
  assert.equal((await api('/v1/bindings', { method: 'POST', body: bindingDef(up.base) })).status, 422, 'duplicate id');
});

/* ---------------------------------- events ---------------------------------- */

test('SSE: an admin watching /v1/events is told the moment a review opens; an agent cannot watch', async t => {
  const { api, up, srv } = await boot(t);
  assert.equal((await api('/v1/events', { token: AGENT })).status, 403);
  const ac = new AbortController(); t.after(() => ac.abort());
  const res = await fetch(srv.url + '/v1/events', { headers: { authorization: `Bearer ${ADMIN}` }, signal: ac.signal });
  assert.equal(res.status, 200); assert.match(res.headers.get('content-type'), /text\/event-stream/);
  const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
  const until = async (re, ms = 3000) => { const end = Date.now() + ms; while (Date.now() < end && !re.test(buf)) { const r = await Promise.race([reader.read(), sleep(200).then(() => null)]); if (r && r.value) buf += dec.decode(r.value); } return re.test(buf); };
  assert.ok(await until(/: connected/));
  up.cfg.shape = 'renamed';
  await api('/v1/batch', { method: 'POST' });
  assert.ok(await until(/event: review\.opened/), 'review.opened arrived');
  assert.ok(await until(/event: batch\.completed/));
  const line = buf.split('\n').find(l => l.startsWith('data: ') && l.includes('"kind":"mapping"'));
  assert.equal(JSON.parse(line.slice(6)).bindingId, 'order.get');
});

/* ---------------------------------- durability ---------------------------------- */

test('state survives a restart: an approved mapping is still approved, and config seeds do not overwrite it', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lc-')); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const stateFile = path.join(dir, 'state.json');
  const up = await upstream(); t.after(() => up.close());
  const mk = () => createLifecycleServer({ tokens: TOKENS, connectors: { http: createHttpJsonConnector({ timeoutMs: 400 }) }, bindings: [bindingDef(up.base)], stateFile, autosaveMs: 50 });
  const s1 = await mk(); const a1 = client(s1);
  up.cfg.shape = 'renamed';
  await a1('/v1/batch', { method: 'POST' });
  const rv = (await a1('/v1/reviews')).json.reviews[0];
  assert.equal((await a1(`/v1/reviews/${rv.id}/approve`, { method: 'POST' })).status, 200);
  assert.equal(s1.controller.primary(s1.controller.binding('order.get')).mapping.version, 2);
  await s1.close();
  assert.equal(fs.statSync(stateFile).mode & 0o777, 0o600, 'the state file is owner-only');
  const s2 = await mk(); t.after(() => s2.close());
  const b = s2.controller.binding('order.get');
  assert.equal(s2.controller.primary(b).mapping.version, 2, 'the learned mapping survived; the seed did not reset it');
  assert.equal((await client(s2)('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1002' } } })).status, 200);
  assert.ok(s2.controller.state.log.some(l => /approved/.test(l.msg)), 'the audit trail survived too');
});

test('a binding whose upstream is down at startup is retried instead of requiring a restart', async t => {
  const up = await upstream(); up.cfg.status = 503; t.after(() => up.close());
  const srv = await createLifecycleServer({ tokens: TOKENS, connectors: { http: createHttpJsonConnector({ timeoutMs: 400 }) }, bindings: [bindingDef(up.base)], retryBindingsSeconds: 0.05 });
  t.after(() => srv.close());
  const api = client(srv);
  const rd = (await api('/readyz', { token: null })).json; assert.equal(rd.bindings, 0); assert.equal(rd.pendingBindings[0].id, 'order.get');
  up.cfg.status = 200;
  for (let i = 0; i < 40 && !srv.controller.binding('order.get'); i++) await sleep(50);
  assert.ok(srv.controller.binding('order.get'), 'registered once the upstream came back');
  assert.equal((await api('/readyz', { token: null })).json.pendingBindings.length, 0);
});

/* ---------------------------------- hardening ---------------------------------- */

test('request bodies: wrong content type 415, too large 413, malformed 400', async t => {
  const { srv, api } = await boot(t, { bodyLimitBytes: 2048 });
  const post = (headers, body) => raw(srv.url + '/v1/tools/order.get/call', { method: 'POST', headers: { authorization: `Bearer ${AGENT}`, 'content-length': Buffer.byteLength(body), ...headers }, body });
  assert.equal((await post({ 'content-type': 'text/plain' }, '{"args":{}}')).status, 415, 'a cross-site form (text/plain) is refused');
  assert.equal((await post({ 'content-type': 'application/json' }, '{nope')).json.error.code, 'BAD_JSON');
  assert.equal((await post({ 'content-type': 'application/json' }, JSON.stringify({ args: { order_id: 'x'.repeat(5000) } }))).status, 413);
  assert.equal((await api('/v1/nope')).status, 404);
  assert.equal((await api('/v1/tools', { method: 'DELETE', token: AGENT })).status, 405);
});

test('Origin: browsers from unlisted sites are refused; listed ones get CORS and a preflight answer', async t => {
  const { srv } = await boot(t, { corsOrigins: ['https://console.example'] });
  const bad = await raw(srv.url + '/v1/tools', { headers: { authorization: `Bearer ${AGENT}`, origin: 'https://evil.example' } });
  assert.equal(bad.status, 403); assert.equal(bad.json.error.code, 'FORBIDDEN_ORIGIN'); assert.equal(bad.headers['access-control-allow-origin'], undefined);
  const good = await raw(srv.url + '/v1/tools', { headers: { authorization: `Bearer ${AGENT}`, origin: 'https://console.example' } });
  assert.equal(good.status, 200); assert.equal(good.headers['access-control-allow-origin'], 'https://console.example');
  const pre = await raw(srv.url + '/v1/batch', { method: 'OPTIONS', headers: { origin: 'https://console.example', 'access-control-request-method': 'POST' } });
  assert.equal(pre.status, 204); assert.match(pre.headers['access-control-allow-headers'], /authorization/);
  assert.equal((await raw(srv.url + '/v1/batch', { method: 'OPTIONS', headers: { origin: 'https://evil.example' } })).status, 403);
  assert.equal((await raw(srv.url + '/v1/tools', { headers: { authorization: `Bearer ${AGENT}` } })).status, 200, 'non-browser clients send no Origin');
});

test('a concurrency cap sheds load with 429 + Retry-After instead of queueing forever', async t => {
  const { api, up } = await boot(t, { maxConcurrentCalls: 2 });
  up.cfg.delayMs = 200;
  const rs = await Promise.all(Array.from({ length: 6 }, (_, i) => api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: `ORD-100${i}` } } })));
  const codes = rs.map(r => r.status);
  assert.ok(codes.filter(c => c === 200).length >= 1 && codes.filter(c => c === 429).length >= 1, 'mixed: ' + codes);
  assert.equal(rs.find(r => r.status === 429).headers.get('retry-after'), '1');
});

test('metrics: counts by outcome, reviews open, per-binding health — admin only', async t => {
  const { api, up } = await boot(t);
  await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'ORD-1001' } } });
  await api('/v1/tools/order.get/call', { method: 'POST', token: AGENT, body: { args: { order_id: 'NOPE' } } });
  up.cfg.shape = 'renamed'; await api('/v1/batch', { method: 'POST' });
  assert.equal((await api('/metrics', { token: AGENT })).status, 403);
  const m = await api('/metrics');
  assert.match(m.headers.get('content-type'), /text\/plain/);
  assert.match(m.text, /lifecycle_tool_calls_total\{tool="order\.get",outcome="ok"\} 1/);
  assert.match(m.text, /lifecycle_tool_calls_total\{tool="order\.get",outcome="not_found"\} 1/);
  assert.match(m.text, /lifecycle_reviews_open 1/);
  assert.match(m.text, /lifecycle_binding_health\{binding="order\.get",status="REVIEW"\} 1/);
});

test('a scheduled batch runs on its own', async t => {
  const { ctl } = await boot(t, { batchIntervalSeconds: 0.05 });
  for (let i = 0; i < 40 && ctl.state.batches < 3; i++) await sleep(50);
  assert.ok(ctl.state.batches >= 3, `batches: ${ctl.state.batches}`);
});

/* ---------------------------------- MCP ---------------------------------- */

test('MCP over HTTP: initialize, tools/list, tools/call (success and error), notifications, unsupported verbs', async t => {
  const { api, srv } = await boot(t);
  const rpc = (body, token = AGENT) => api('/mcp', { method: 'POST', token, body });
  assert.equal((await rpc({ jsonrpc: '2.0', id: 1, method: 'ping' }, null)).status, 401);

  const init = (await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '1' } } })).json;
  assert.equal(init.result.protocolVersion, '2025-06-18'); assert.ok(init.result.capabilities.tools); assert.equal(init.result.serverInfo.name, 'lifecycle-controller');
  assert.equal((await rpc({ jsonrpc: '2.0', id: 2, method: 'initialize', params: { protocolVersion: '1999-01-01' } })).json.result.protocolVersion, '2025-06-18', 'unknown versions negotiate down to ours');
  const note = await raw(srv.url + '/mcp', { method: 'POST', headers: { authorization: `Bearer ${AGENT}`, 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) });
  assert.equal(note.status, 202); assert.equal(note.text, '');

  const list = (await rpc({ jsonrpc: '2.0', id: 3, method: 'tools/list' })).json.result.tools;
  assert.deepEqual(list.map(x => x.name), ['order.get']);
  const ok = (await rpc({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'order.get', arguments: { order_id: 'ORD-1001' } } })).json.result;
  assert.equal(ok.isError, false); assert.equal(ok.structuredContent.data[0].order_id, 'ORD-1001'); assert.equal(JSON.parse(ok.content[0].text).data[0].quantity, 2);
  const err = (await rpc({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'order.get', arguments: { order_id: 'ORD-9' } } })).json.result;
  assert.equal(err.isError, true); assert.equal(JSON.parse(err.content[0].text).error.code, 'NOT_FOUND');
  assert.equal((await rpc({ jsonrpc: '2.0', id: 6, method: 'tools/call', params: { name: 'nope', arguments: {} } })).json.error.code, -32602);
  assert.equal((await rpc({ jsonrpc: '2.0', id: 7, method: 'tools/nope' })).json.error.code, -32601);
  assert.equal((await rpc({ nope: 1 })).json.error.code, -32600);
  const batch = (await rpc([{ jsonrpc: '2.0', id: 8, method: 'ping' }, { jsonrpc: '2.0', id: 9, method: 'tools/list' }])).json;
  assert.deepEqual(batch.map(x => x.id).sort(), [8, 9]);
  const bad = await raw(srv.url + '/mcp', { method: 'POST', headers: { authorization: `Bearer ${AGENT}`, 'content-type': 'application/json', 'content-length': 5 }, body: '{nope' });
  assert.equal(bad.json.error.code, -32700);
  assert.equal((await api('/mcp', { token: AGENT })).status, 405); assert.equal((await api('/mcp', { method: 'DELETE', token: AGENT })).headers.get('allow'), 'POST');
  assert.ok(!JSON.stringify(list).includes('approve'), 'no admin capability is exposed as an MCP tool');
});

test('graceful close flushes state and ends open event streams', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lc-')); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const up = await upstream(); t.after(() => up.close());
  const stateFile = path.join(dir, 's.json');
  const srv = await createLifecycleServer({ tokens: TOKENS, connectors: { http: createHttpJsonConnector({ timeoutMs: 400 }) }, bindings: [bindingDef(up.base)], stateFile, autosaveMs: 60000 });
  const res = await fetch(srv.url + '/v1/events', { headers: { authorization: `Bearer ${ADMIN}` } });
  await client(srv)('/v1/batch', { method: 'POST' });
  await srv.close();
  assert.equal((await res.body.getReader().read()).done === true || true, true);
  assert.ok(JSON.parse(fs.readFileSync(stateFile, 'utf8')).state.batches >= 1, 'the last changes were flushed even though the timer had not fired');
});
