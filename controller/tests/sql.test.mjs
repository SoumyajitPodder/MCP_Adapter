import test from 'node:test';
import assert from 'node:assert/strict';
import { createController, createSqlConnector } from '../index.js';
import { truth, orderContract, openReview, testClock } from './helpers.mjs';

let sqlite = null;
try { sqlite = await import('node:sqlite'); } catch { /* older Node: these tests skip */ }
const skip = sqlite ? false : 'node:sqlite is not available on this Node version';

// A real SQLite database standing in for "a legacy table".
async function boot() {
  const db = new sqlite.DatabaseSync(':memory:');
  db.exec('CREATE TABLE orders (order_id TEXT, status TEXT, customer_id TEXT, created_at TEXT)');
  const ins = db.prepare('INSERT INTO orders VALUES (?,?,?,?)');
  for (let i = 0; i < 6; i++) { const t = truth(i); ins.run(t.id, t.status, t.cust, t.at); }
  const query = async sql => db.prepare(sql).all();
  const ctl = createController({ clock: testClock(), connectors: { sql: createSqlConnector({ query }) } });
  // No mapping supplied: the controller must infer one from the live table.
  const r = await ctl.addBinding({ id: 'orders', connector: 'sql', source: { sql: 'SELECT * FROM orders ORDER BY order_id LIMIT {limit}' }, contract: orderContract() }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  return { db, ctl, query };
}

test('sql: a mapping is inferred from a real table, and the table is served', { skip }, async () => {
  const { ctl } = await boot();
  await ctl.runBatch();
  assert.equal(ctl.health('orders'), 'PASS');
  assert.equal((await ctl.liveCall('orders')).data[0].order_id, 'ORD-1000');
});

test('sql: new rows and a new column are absorbed; a renamed column goes to review', { skip }, async () => {
  const { db, ctl } = await boot();
  const t = truth(9); db.prepare('INSERT INTO orders VALUES (?,?,?,?)').run(t.id, t.status, t.cust, t.at);
  await ctl.runBatch(); assert.equal(ctl.health('orders'), 'PASS');
  db.exec('ALTER TABLE orders ADD COLUMN channel TEXT');
  await ctl.runBatch(); assert.equal(ctl.health('orders'), 'PASS', 'an added column is harmless');
  db.exec('ALTER TABLE orders RENAME COLUMN status TO order_status');
  await ctl.runBatch();
  assert.equal(ctl.health('orders'), 'REVIEW');
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv.renames.some(r => r.from === 'status' && r.to === 'order_status'));
  assert.equal((await ctl.approve(rv.id)).ok, true);
  assert.equal(ctl.health('orders'), 'PASS');
  assert.equal((await ctl.liveCall('orders')).data[0].status, 'in_progress', 'served under the canonical name, unchanged');
});

test('sql: a dropped table is a gone upstream; a locked database is just an outage', { skip }, async () => {
  const { db, ctl } = await boot();
  let lock = false;
  const flaky = createSqlConnector({ query: async s => { if (lock) throw new Error('database is locked'); return db.prepare(s).all(); } });
  ctl.registerConnector('sql', flaky);
  lock = true; await ctl.runBatch();
  assert.equal(ctl.health('orders'), 'FAIL'); assert.equal(ctl.state.reviews.length, 0, 'a lock is not a migration');
  lock = false; await ctl.runBatch(); assert.equal(ctl.health('orders'), 'PASS');
  db.exec('DROP TABLE orders');
  await ctl.runBatch();
  assert.equal(ctl.health('orders'), 'FAIL');
  const mr = openReview(ctl, 'migration');
  assert.equal(mr.stage, 'no_target'); assert.equal(mr.reasonKey, 'gone');
});
