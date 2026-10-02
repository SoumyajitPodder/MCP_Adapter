import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createController, createHttpJsonConnector } from '../index.js';
import { DAY_MS } from '../utils.js';
import { truth, orderContract, orderMapping, openReview, T0, testClock } from './helpers.mjs';

// A real HTTP server on a real port, serving nested JSON, a v3 with an
// envelope, a Sunset header, errors, and slow responses on demand.
async function upstream() {
  const cfg = { v2Shape: 'orig', v2Status: 200, v3Status: 200, sunset: null, delayMs: 0 };
  const server = http.createServer(async (req, res) => {
    const m = /^\/(v2|v3)\/orders\/(\d+)$/.exec(req.url);
    if (!m) { res.writeHead(404); return res.end('{}'); }
    const ver = m[1], t = truth(Number(m[2]));
    if (cfg.delayMs) await new Promise(r => setTimeout(r, cfg.delayMs));
    const status = cfg[ver + 'Status'];
    const headers = { 'content-type': 'application/json' };
    if (ver === 'v2' && cfg.sunset) headers.Sunset = new Date(cfg.sunset).toUTCString();
    if (status !== 200) { res.writeHead(status, headers); return res.end('{"error":"nope"}'); }
    const body = ver === 'v2'
      ? { orderId: t.id, [cfg.v2Shape === 'renamed' ? 'state' : 'orderState']: t.status, customer: { id: t.cust }, createdAt: t.at }
      : { data: { order_id: t.id, order_state: t.status, customer_id: t.cust, created_at: t.at } };
    res.writeHead(200, headers); res.end(JSON.stringify(body));
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const probes = v => [0, 1, 2, 3, 4, 5].map(i => `${base}/${v}/orders/${i}`);
  return { cfg, base, probes, close: () => { server.closeAllConnections?.(); server.close(); } };
}

async function boot(t, connectorOpts = {}) {
  const up = await upstream();
  t.after(up.close);
  const source = { versions: { v2: { probes: up.probes('v2') } } };
  const ctl = createController({ clock: testClock(), connectors: { http: createHttpJsonConnector(connectorOpts) } });
  const m = orderMapping({ customer_id: 'customer.id' });
  const r = await ctl.addBinding({ id: 'order.get', connector: 'http', source, contract: orderContract(), mapping: m, version: 'v2' }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  return { ...up, ctl, source };
}

test('http: reads nested JSON from a real server and serves canonical output', async t => {
  const { ctl } = await boot(t);
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS');
  const call = await ctl.liveCall('order.get');
  assert.deepEqual(Object.keys(call.data[0]), ['order_id', 'status', 'customer_id', 'created_at']);
  assert.equal(call.data[0].customer_id, '48000');
});

test('http: a renamed field is caught and goes through review', async t => {
  const { ctl, cfg } = await boot(t);
  cfg.v2Shape = 'renamed';
  await ctl.runBatch();
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv, 'review opened'); assert.ok(rv.conf >= 0.6);
  await ctl.approve(rv.id);
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('http: a Sunset header drives a full guided migration to an enveloped v3', async t => {
  const { ctl, cfg, probes, source } = await boot(t);
  cfg.sunset = T0 + 3 * DAY_MS;
  source.versions.v3 = { probes: probes('v3') };
  await ctl.runBatch();
  const mr = openReview(ctl, 'migration');
  assert.match(mr.reasonText, /retired in 3 days/);
  assert.equal(mr.preview.ok, true);
  assert.equal(mr.preview.mapping.unwrap, 'data', 'the envelope was detected and unwrapped');
  assert.equal((await ctl.beginMigration('order.get', 'v3')).ok, true);
  await ctl.promote('order.get', 'order.get/a2'); await ctl.runBatch(); await ctl.promote('order.get', 'order.get/a2');
  assert.equal(mr.status, 'resolved');
  cfg.v2Status = 410;                       // the old version is now switched off for real
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS', 'v3 keeps serving after v2 is gone');
  assert.match((await ctl.liveCall('order.get'))._meta.served_by, /^v3/);
});

test('http: 503s and timeouts are outages, never drift', async t => {
  const { ctl, cfg } = await boot(t, { timeoutMs: 60 });
  cfg.v2Status = 503; await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'FAIL'); assert.equal(ctl.state.reviews.length, 0);
  cfg.v2Status = 200; cfg.delayMs = 400; await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'FAIL'); assert.equal(ctl.state.reviews.length, 0);
  assert.equal(ctl.state.metrics.blocked, 0);
  cfg.delayMs = 0; await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('http: a 410 with no other version is the honest dead end', async t => {
  const { ctl, cfg } = await boot(t);
  cfg.v2Status = 410; await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'FAIL');
  assert.equal(openReview(ctl, 'migration').stage, 'no_target');
});
