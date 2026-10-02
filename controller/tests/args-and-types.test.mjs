import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createController, createHttpJsonConnector, createSqlConnector, createDelimitedFileConnector, createRemoteConnector } from '../index.js';
import { parseDate } from '../validator.js';
import { inferFmt } from '../utils.js';
import { testClock, truth, orderContract, openReview } from './helpers.mjs';

/* ------------------------- richer contract types ------------------------- */

const richContract = () => ({ version: '1.0.0', fields: [
  { name: 'sku', type: 'string' }, { name: 'quantity', type: 'integer' }, { name: 'price', type: 'number' },
  { name: 'active', type: 'boolean' }, { name: 'opened', type: 'date' }, { name: 'updated', type: 'datetime' },
  { name: 'tracking_number', type: 'string', optional: true }, { name: 'status', type: 'enum', values: ['open', 'closed'] }] });
const richRow = i => ({ sku: `SKU-${100 + i}`, quantity: i + 1, price: 9.5 + i, active: i % 2 === 1, opened: `2026-03-${10 + i}`,
  updated: `2026-03-25T10:0${i}:00+02:00`, tracking_number: i % 2 ? `TRK${i}` : null, status: i % 2 ? 'closed' : 'open' });

function argsConnector(db) {
  return { async fetch(b, v, { limit, args }) {
    if (db.status) return { status: db.status, records: [], error: db.error };
    db.calls.push(args || null);
    if (args) {
      if (!args.sku) return { status: 400, records: [], error: 'sku is required' };
      const hit = db.rows.filter(r => r[db.keyField || 'sku'] === args.sku);
      return hit.length ? { status: 200, records: hit.map(r => ({ ...r })) } : { status: 404, records: [] };
    }
    return { status: 200, records: db.rows.slice(0, limit).map(r => ({ ...r })) };
  } };
}
async function richSetup(rowsFn = richRow, n = 6) {
  const db = { rows: Array.from({ length: n }, (_, i) => rowsFn(i)), calls: [] };
  const ctl = createController({ clock: testClock(), connectors: { m: argsConnector(db) } });
  const r = await ctl.addBinding({ id: 'inventory', connector: 'm', contract: richContract() }, { quiet: true }); // mapping inferred
  assert.equal(r.ok, true, r.reason);
  return { ctl, db };
}

test('types: integer, number, boolean, date, datetime-with-offset and optional all round-trip', async () => {
  const { ctl } = await richSetup();
  await ctl.runBatch(); assert.equal(ctl.health('inventory'), 'PASS');
  const r = await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } });
  assert.deepEqual(r.data, [{ sku: 'SKU-101', quantity: 2, price: 10.5, active: true, opened: '2026-03-11', updated: '2026-03-25T08:01:00Z', tracking_number: 'TRK1', status: 'closed' }]);
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-100' } })).data[0].tracking_number, null, 'optional + null reads as null');
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-100' } })).data[0].active, false, 'false is a value, not "missing"');
});

test('types: a number that turns into a string is blocked, not coerced or guessed', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(r => ({ ...r, quantity: String(r.quantity) }));
  const r = await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } });
  assert.equal(r.error.code, 'DRIFT_BLOCKED');
});

test('dates: offsets are normalised exactly, impossible dates are refused', () => {
  assert.equal(parseDate('2026-03-25T10:00:00+02:00', 'ISO'), '2026-03-25T08:00:00Z');
  assert.equal(parseDate('2026-03-25T23:30:00-05:00', 'ISO'), '2026-03-26T04:30:00Z');
  assert.equal(parseDate('2026-03-25T10:00:00.987Z', 'ISO'), '2026-03-25T10:00:00Z');
  assert.throws(() => parseDate('2026-02-31T00:00:00Z', 'ISO'));
  assert.throws(() => parseDate('2026-03-25T24:00:00Z', 'ISO'));
  assert.equal(inferFmt('2026-03-25'), 'ISO_DATE');
});

test('nulls are not drift: a nullable field that is null in some records and set in others stays healthy', async () => {
  const { ctl } = await richSetup(); // tracking_number alternates null / string
  await ctl.runBatch(); await ctl.runBatch();
  assert.equal(ctl.health('inventory'), 'PASS');
  assert.equal(ctl.state.metrics.blocked, 0);
  assert.equal(ctl.state.reviews.length, 0);
});

test('a REQUIRED field that arrives null fails closed', async () => {
  const { ctl, db } = await richSetup();
  db.rows[1].quantity = null;
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } })).error.code, 'DRIFT_BLOCKED');
});

test('an OPTIONAL field that is renamed is caught for review, not silently read as null', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(({ tracking_number, ...rest }) => ({ ...rest, tracking_id: tracking_number }));
  await ctl.runBatch();
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv && rv.renames.some(x => x.from === 'tracking_number' && x.to === 'tracking_id'), 'proposed as a rename');
  assert.equal((await ctl.approve(rv.id)).ok, true);
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } })).data[0].tracking_number, 'TRK1', 'the value follows the rename');
});

test('an optional field that simply disappears is absorbed as null, but the regression check still surfaces the lost values', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(({ tracking_number, ...rest }) => rest);
  await ctl.runBatch();
  assert.ok(ctl.state.log.some(l => /optional field "tracking_number" is absent/.test(l.msg)), 'audited');
  const run = ctl.primary(ctl.binding('inventory')).lastRun;
  assert.equal(run.overall, 'COMPATIBLE', 'nothing for a person to approve...');
  assert.equal(ctl.health('inventory'), 'FAIL', '...but known values turning into null is not silent');
  assert.ok(run.stages[4].lines.some(l => /SKU-101\.tracking_number: was TRK1, now null/.test(l)));
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } })).data[0].tracking_number, null, 'calls keep working, reading null');
});

/* ------------------------- parameterized calls ------------------------- */

test('args: the call returns the record that was asked for, not a sample', async () => {
  const { ctl, db } = await richSetup();
  const r = await ctl.liveCall('inventory', { args: { sku: 'SKU-104' } });
  assert.equal(r.data.length, 1); assert.equal(r.data[0].sku, 'SKU-104');
  assert.deepEqual(db.calls.at(-1), { sku: 'SKU-104' });
});

test('args: "no such record" and "bad arguments" are answers — no drift, no outage, no audit noise', async () => {
  const { ctl } = await richSetup();
  await ctl.runBatch();
  const before = { seq: ctl.state.seq, health: ctl.health('inventory'), blocked: ctl.state.metrics.blocked };
  const nf = await ctl.liveCall('inventory', { args: { sku: 'NOPE' } });
  const br = await ctl.liveCall('inventory', { args: { other: 1 } });
  assert.deepEqual([nf.error.code, nf.error.status], ['NOT_FOUND', 404]);
  assert.deepEqual([br.error.code, br.error.status, br.error.message], ['BAD_REQUEST', 400, 'sku is required']);
  assert.deepEqual({ seq: ctl.state.seq, health: ctl.health('inventory'), blocked: ctl.state.metrics.blocked }, before, 'nothing was logged, nothing changed');
});

test('args: drift met at runtime fails the call closed AND opens a review immediately', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(({ status, ...rest }) => ({ ...rest, state: status })); // a probable rename
  const r = await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } });
  assert.equal(r.error.code, 'DRIFT_BLOCKED');
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv, 'no need to wait for the next batch'); assert.equal(r.error.review, rv.id);
  await ctl.approve(rv.id);
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } })).data[0].status, 'closed');
});

test('args: a deterministic case-style rename is absorbed and reported in _meta', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(({ tracking_number, ...rest }) => ({ ...rest, trackingNumber: tracking_number }));
  db.rows = db.rows.map(r => ({ ...r, sku: r.sku }));
  const r = await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } });
  assert.equal(r.data[0].tracking_number, 'TRK1');
  assert.ok(r._meta.absorbed.some(a => /case-style rename/.test(a)));
});

test('args: an upstream outage on a call is reported as unavailable and opens no review', async () => {
  const { ctl, db } = await richSetup();
  db.status = 503;
  const r = await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } });
  assert.deepEqual([r.error.code, r.error.status], ['UPSTREAM_UNAVAILABLE', 503]);
  assert.equal(ctl.state.reviews.length, 0);
});

/* ------------------------- connectors use args safely ------------------------- */

const orderRow = t => ({ order_id: t.id, status: t.status, customer_id: t.cust, created_at: t.at });

test('http: arguments are URL-encoded and can never change the path or host', async t => {
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push(req.url);
    const m = /^\/orders\/([^/?]+)$/.exec(req.url);
    const idx = m ? [0, 1, 2, 3, 4, 5].find(i => truth(i).id === decodeURIComponent(m[1])) : undefined;
    res.writeHead(idx === undefined ? 404 : 200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(idx === undefined ? {} : orderRow(truth(idx))));
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r)); t.after(() => { server.closeAllConnections?.(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const ctl = createController({ clock: testClock(), connectors: { http: createHttpJsonConnector() } });
  const r = await ctl.addBinding({ id: 'orders', connector: 'http', contract: orderContract(), inputs: { properties: { order_id: { type: 'string' } } },
    source: { versions: { v1: { probes: [0, 1, 2].map(i => `${base}/orders/${truth(i).id}`), lookup: { url: `${base}/orders/{order_id}` } } } } }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  assert.equal((await ctl.liveCall('orders', { args: { order_id: 'ORD-1002' } })).data[0].order_id, 'ORD-1002');
  seen.length = 0;
  for (const evil of ['../admin', 'ORD-1000/../../etc/passwd', 'x?debug=1', 'a#b', 'ORD-1000%2e%2e']) {
    const out = await ctl.liveCall('orders', { args: { order_id: evil } });
    assert.equal(out.error.code, 'NOT_FOUND', evil);
  }
  assert.ok(seen.length === 5 && seen.every(u => /^\/orders\/[A-Za-z0-9%._-]+$/.test(u) && !u.includes('..%2F') === true || /^\/orders\/[^/]+$/.test(u)), 'every request stayed a single path segment under /orders/: ' + seen.join(' '));
  assert.ok(seen.every(u => u.split('/').length === 3), 'no argument produced an extra path segment');
  const n = seen.length;
  assert.equal((await ctl.liveCall('orders', { args: { wrong_name: '1' } })).error.code, 'BAD_REQUEST');
  assert.equal(seen.length, n, 'a call with a missing argument never reaches the upstream');
});

let sqlite = null; try { sqlite = await import('node:sqlite'); } catch { /* skip */ }
test('sql: arguments are bound parameters — injection attempts are inert', { skip: sqlite ? false : 'node:sqlite unavailable' }, async () => {
  const db = new sqlite.DatabaseSync(':memory:');
  db.exec('CREATE TABLE orders (order_id TEXT, status TEXT, customer_id TEXT, created_at TEXT)');
  const ins = db.prepare('INSERT INTO orders VALUES (?,?,?,?)'); for (let i = 0; i < 6; i++) { const t = truth(i); ins.run(t.id, t.status, t.cust, t.at); }
  const captured = [];
  const query = async (sql, values = []) => { captured.push({ sql, values }); return db.prepare(sql).all(...values); };
  const ctl = createController({ clock: testClock(), connectors: { sql: createSqlConnector({ query }) } });
  const r = await ctl.addBinding({ id: 'orders', connector: 'sql', contract: orderContract(), source: { versions: { v1: {
    sql: 'SELECT * FROM orders ORDER BY order_id LIMIT {limit}', lookup: { sql: 'SELECT * FROM orders WHERE order_id = :order_id', params: ['order_id'] } } } } }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  assert.equal((await ctl.liveCall('orders', { args: { order_id: 'ORD-1003' } })).data[0].order_id, 'ORD-1003');
  for (const evil of ["' OR '1'='1", "ORD-1000'; DROP TABLE orders; --", '1 UNION SELECT * FROM orders']) {
    captured.length = 0;
    assert.equal((await ctl.liveCall('orders', { args: { order_id: evil } })).error.code, 'NOT_FOUND', evil);
    assert.ok(!captured[0].sql.includes(evil) && captured[0].sql.includes('?') && captured[0].values[0] === evil, 'the value traveled separately from the SQL text');
  }
  assert.equal(db.prepare('SELECT count(*) AS n FROM orders').get().n, 6, 'the table is intact');
});

test('csv: lookup is an exact match; absent is 404, unusable is 400', async () => {
  const text = 'item_id,status,site_id,updated_at\nITM-1,AVAIL,S1,2026-08-01T06:00:00Z\nITM-2,RSVD,S2,2026-08-01T06:00:00Z\nITM-3,DECOM,S3,2026-08-01T06:00:00Z';
  const c = createDelimitedFileConnector({ readText: async () => text });
  const b = { source: { versions: { v1: { path: '/x', lookup: { column: 'item_id', arg: 'item_id' } } } } };
  assert.equal((await c.fetch(b, 'v1', { args: { item_id: 'ITM-2' } })).records[0].site_id, 'S2');
  assert.equal((await c.fetch(b, 'v1', { args: { item_id: 'ITM' } })).status, 404, 'no partial matching');
  assert.equal((await c.fetch(b, 'v1', { args: { nope: 1 } })).status, 400);
  assert.equal((await c.fetch({ source: { path: '/x' } }, 'v1', { args: { a: 1 } })).status, 400, 'a source with no lookup refuses arguments');
});

/* ------------------------- the remote (any-language) connector ------------------------- */

test('remote: a service in any language can be the connector — fetch, args, versions, failures', async t => {
  const seen = []; let mode = 'ok';
  const server = http.createServer((req, res) => {
    let body = ''; req.on('data', d => { body += d; });
    req.on('end', () => {
      const j = body ? JSON.parse(body) : {}; seen.push({ path: req.url, ...j });
      if (mode === 'boom') { res.writeHead(500); return res.end('oops'); }
      if (mode === 'html') { res.writeHead(200); return res.end('<html>'); }
      res.writeHead(200, { 'content-type': 'application/json' });
      if (req.url.endsWith('/versions')) return res.end(JSON.stringify({ versions: ['v1'] }));
      const rows = [0, 1, 2, 3, 4, 5].map(i => orderRow(truth(i)));
      if (j.args) { const hit = rows.filter(r => r.order_id === j.args.order_id); return res.end(JSON.stringify(hit.length ? { status: 200, records: hit } : { status: 404, records: [] })); }
      res.end(JSON.stringify({ status: 200, records: rows.slice(0, j.limit) }));
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r)); t.after(() => { server.closeAllConnections?.(); server.close(); });
  const conn = createRemoteConnector({ url: `http://127.0.0.1:${server.address().port}/lifecycle`, headers: { 'x-api-key': 'k' } });
  assert.equal(typeof conn.operatorMapping, 'undefined', 'a mapping is only offered if you say your service can supply one');
  assert.equal(typeof createRemoteConnector({ url: 'http://x', capabilities: { operatorMapping: true } }).operatorMapping, 'function');
  const ctl = createController({ clock: testClock(), connectors: { remote: conn } });
  const r = await ctl.addBinding({ id: 'orders', connector: 'remote', contract: orderContract(), source: { system: 'mainframe' } }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  assert.ok(seen.some(s => s.path === '/lifecycle/versions'), 'the versions endpoint was asked');
  assert.deepEqual(ctl.binding('orders').knownVersions, ['v1']);
  assert.equal((await ctl.liveCall('orders', { args: { order_id: 'ORD-1004' } })).data[0].order_id, 'ORD-1004');
  assert.deepEqual(seen.filter(s => s.args).at(-1).args, { order_id: 'ORD-1004' }, 'arguments were forwarded');
  assert.deepEqual(seen.at(-1).binding.source, { system: 'mainframe' });
  assert.equal((await ctl.liveCall('orders', { args: { order_id: 'ORD-9' } })).error.code, 'NOT_FOUND');
  mode = 'boom'; await ctl.runBatch(); assert.equal(ctl.health('orders'), 'FAIL'); assert.equal(ctl.state.reviews.length, 0, 'a failing service is an outage, not drift');
  mode = 'html'; await ctl.runBatch(); assert.equal(ctl.health('orders'), 'FAIL');
  mode = 'ok'; await ctl.runBatch(); assert.equal(ctl.health('orders'), 'PASS');
});

test('a connector that returns nothing from listVersions does not crash registration', async () => {
  const ctl = createController({ clock: testClock(), connectors: { m: { async fetch(b, v, { limit }) { return { status: 200, records: [0, 1, 2].map(i => ({ order_id: truth(i).id, status: truth(i).status, customer_id: truth(i).cust, created_at: truth(i).at })).slice(0, limit) }; }, async listVersions() { return null; } } } });
  const r = await ctl.addBinding({ id: 'orders', connector: 'm', contract: orderContract() }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  assert.deepEqual(ctl.binding('orders').knownVersions, ['v1']);
});

test('registration: an optional field the upstream does not have yet reads as null instead of blocking registration', async () => {
  const rows = [0, 1, 2].map(i => ({ sku: `S${i}`, quantity: i }));
  const ctl = createController({ clock: testClock(), connectors: { m: { async fetch(b, v, { limit }) { return { status: 200, records: rows.slice(0, limit).map(r => ({ ...r })) }; } } } });
  const contract = { fields: [{ name: 'sku', type: 'string' }, { name: 'quantity', type: 'integer' }, { name: 'backorder_eta', type: 'string', optional: true }] };
  assert.equal((await ctl.addBinding({ id: 'inv', connector: 'm', contract }, { quiet: true })).ok, true);
  assert.equal((await ctl.liveCall('inv')).data[0].backorder_eta, null);
  const strict = { fields: [...contract.fields.slice(0, 2), { name: 'backorder_eta', type: 'string' }] };
  const r = await ctl.addBinding({ id: 'inv2', connector: 'm', contract: strict }, { quiet: true });
  assert.equal(r.ok, false, 'a REQUIRED field that is absent still blocks registration'); assert.match(r.reason, /backorder_eta/);
});

test('evidence: an abbreviation the name metric cannot see (quantity_available -> qty_avail) is recognised by its VALUES', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(({ quantity, ...rest }) => ({ ...rest, qty: quantity }));          // 'quantity' -> 'qty' alone would also do; use a harder one:
  db.rows = db.rows.map(({ qty, ...rest }) => ({ ...rest, q_avail: qty }));
  await ctl.runBatch();
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv, 'proposed for review, not dismissed as breaking');
  const r = rv.renames.find(x => x.from === 'quantity');
  assert.equal(r.to, 'q_avail'); assert.ok(r.score >= 0.6 && r.value === 1, `value evidence is 1 (got ${r.value}, score ${r.score})`);
  assert.equal((await ctl.approve(rv.id)).ok, true);
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-103' } })).data[0].quantity, 4);
});

test('evidence cuts both ways: a new field that holds DIFFERENT values than the old one is not mistaken for it', async () => {
  const { ctl, db } = await richSetup();
  db.rows = db.rows.map(({ quantity, ...rest }) => ({ ...rest, q_avail: quantity * 2 }));   // same shape, different numbers
  await ctl.runBatch();
  assert.equal(openReview(ctl, 'mapping'), undefined, 'no rename proposed');
  assert.equal(ctl.health('inventory'), 'FAIL');
  assert.equal((await ctl.liveCall('inventory', { args: { sku: 'SKU-101' } })).error.code, 'DRIFT_BLOCKED');
});
