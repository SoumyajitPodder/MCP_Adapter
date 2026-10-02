import http from 'node:http';
import { createHttpJsonConnector } from '../../controller/index.js';
import { createLifecycleServer } from '../src/server.js';

export const AGENT = 'agent-token-0123456789', ADMIN = 'admin-token-0123456789';
export const TOKENS = [{ name: 'agent-prod', role: 'agent', token: AGENT }, { name: 'ops-alice', role: 'admin', token: ADMIN }];
export const CONTRACT = { version: '1.0.0', fields: [
  { name: 'order_id', type: 'string' }, { name: 'status', type: 'enum', values: ['in_progress', 'completed', 'cancelled'] },
  { name: 'quantity', type: 'integer' }, { name: 'tracking_number', type: 'string', optional: true }] };
const STATUS = ['in_progress', 'completed', 'cancelled'];
export const row = i => ({ order_id: `ORD-${1000 + i}`, status: STATUS[i % 3], quantity: i + 1, tracking_number: i % 2 ? `TRK${i}` : null });
export const sleep = ms => new Promise(r => setTimeout(r, ms));

// A real HTTP upstream whose behaviour the test can change mid-flight.
export async function upstream() {
  const cfg = { shape: 'orig', status: 200, delayMs: 0, sunset: null, hits: [] };
  const server = http.createServer(async (req, res) => {
    cfg.hits.push(req.url);
    const m = /^\/(v2|v3)\/orders\/([^/?]+)$/.exec(req.url);
    if (cfg.delayMs) await sleep(cfg.delayMs);
    if (cfg.status !== 200) { res.writeHead(cfg.status, { 'content-type': 'application/json' }); return res.end('{}'); }
    const want = m && decodeURIComponent(m[2]);
    const i = want ? [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].find(k => row(k).order_id === want) : undefined;
    if (i === undefined) { res.writeHead(404, { 'content-type': 'application/json' }); return res.end('{}'); }
    const headers = { 'content-type': 'application/json' };
    if (m[1] === 'v2' && cfg.sunset) headers.Sunset = new Date(cfg.sunset).toUTCString();
    let r = row(i);
    if (m[1] === 'v2' && cfg.shape === 'renamed') { const { status, ...rest } = r; r = { ...rest, state: status }; }
    res.writeHead(200, headers); res.end(JSON.stringify(m[1] === 'v3' ? { data: r } : r));
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { cfg, base, close: () => { server.closeAllConnections?.(); return new Promise(r => server.close(r)); } };
}

export const bindingDef = (base, extra = {}) => ({
  id: 'order.get', displayName: 'Order API', description: 'Look up an order by id', connector: 'http', contract: CONTRACT,
  inputs: { properties: { order_id: { type: 'string', description: 'e.g. ORD-1003' } }, required: ['order_id'] },
  source: { versions: { v2: {
    probes: [0, 1, 2, 3, 4, 5].map(i => `${base}/v2/orders/ORD-${1000 + i}`), lookup: { url: `${base}/v2/orders/{order_id}` } } } }, ...extra });

export function client(srv) {
  return async (path, { method = 'GET', token = ADMIN, body, headers = {} } = {}) => {
    const res = await fetch(srv.url + path, { method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(body !== undefined ? { 'content-type': 'application/json' } : {}), ...headers }, body: body !== undefined ? JSON.stringify(body) : undefined });
    const text = await res.text(); let json = null; try { json = JSON.parse(text); } catch { /* not json */ }
    return { status: res.status, headers: res.headers, json, text };
  };
}

export async function boot(t, extra = {}) {
  const up = await upstream();
  const srv = await createLifecycleServer({ tokens: TOKENS, connectors: { http: createHttpJsonConnector({ timeoutMs: 400 }) }, bindings: [bindingDef(up.base)], ...extra });
  t.after(async () => { await srv.close(); await up.close(); });
  return { up, srv, api: client(srv), ctl: srv.controller };
}

// Raw request, for when a header (Origin, Host) must be set that fetch won't allow.
export const raw = (url, { method = 'GET', headers = {}, body } = {}) => new Promise((resolve, reject) => {
  const u = new URL(url);
  const req = http.request({ hostname: u.hostname, port: u.port, path: u.pathname + u.search, method, headers }, res => {
    let d = ''; res.on('data', c => { d += c; }); res.on('end', () => { let json = null; try { json = JSON.parse(d); } catch { /* */ } resolve({ status: res.statusCode, headers: res.headers, json, text: d }); });
  });
  req.on('error', reject); if (body) req.write(body); req.end();
});
