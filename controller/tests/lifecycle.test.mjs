import test from 'node:test';
import assert from 'node:assert/strict';
import { createController } from '../index.js';
import { DAY_MS } from '../utils.js';
import { setup, rowsFor, v2Shape, v3Shape, truth, openReview, T0, testClock, memoryUpstream, orderContract } from './helpers.mjs';

const fresh = () => setup({ v2: { rows: rowsFor(v2Shape) } });

test('a registered binding is healthy and the batch is recorded', async () => {
  const { ctl } = await fresh();
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS');
  assert.equal(ctl.state.batches, 1);
  assert.equal(ctl.state.batchHistory[0].results[0].readiness, 'PASS');
});

test('a case-style rename is absorbed with no human involved', async () => {
  const { ctl, db } = await fresh();
  db.versions.v2.rows = rowsFor(t => ({ order_id: t.id, orderState: t.status, customerId: t.cust, createdAt: t.at }));
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS');
  assert.ok(ctl.state.log.some(l => l.action === 'DRIFT_ABSORBED'));
  assert.equal(openReview(ctl), undefined);
});

test('a probable rename opens a review; approving it restores health', async () => {
  const { ctl, db } = await fresh();
  db.versions.v2.rows = rowsFor(t => ({ orderId: t.id, state: t.status, customerId: t.cust, createdAt: t.at }));
  const seen = []; ctl.on('review.opened', e => seen.push(e.review.id));
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'REVIEW');
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv && rv.conf >= 0.6, 'review carries a confidence');
  assert.deepEqual(seen, [rv.id]);
  assert.equal((await ctl.approve(rv.id)).ok, true);
  assert.equal(ctl.health('order.get'), 'PASS');
  assert.equal(rv.status, 'approved');
});

test('a new enum value needs a human choice, and approval is refused until it is made', async () => {
  const { ctl, db } = await fresh();
  db.versions.v2.rows = rowsFor((t, i) => ({ ...v2Shape(t), orderState: i === 3 ? 'on_hold' : t.status }));
  await ctl.runBatch();
  const rv = openReview(ctl, 'mapping');
  assert.equal(rv.choices.length, 1);
  assert.equal((await ctl.approve(rv.id)).ok, false, 'no choice made yet');
  await ctl.setReviewChoice(rv.id, 0, 'in_progress');
  assert.equal(rv.eval.sandbox.valid, rv.eval.sandbox.total);
  assert.equal((await ctl.approve(rv.id)).ok, true);
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('announced retirement -> guided migration -> v3 becomes current, v2 is history', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape), retireAt: T0 + 3 * DAY_MS }, v3: { rows: rowsFor(v3Shape) } });
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'REVIEW');
  const mr = openReview(ctl, 'migration');
  assert.equal(mr.stage, 'not_started');
  assert.match(mr.reasonText, /retired in 3 days/);
  assert.equal(mr.preview.ok, true);
  assert.equal(mr.canFallback, false, 'this connector cannot supply a mapping on its own');

  assert.equal((await ctl.beginMigration('order.get', 'v3')).ok, true);
  const b = ctl.binding('order.get');
  assert.equal(mr.stage, 'ready_for_canary');
  await ctl.promote('order.get', 'order.get/a2');
  await ctl.runBatch();
  assert.equal(mr.stage, 'ready_for_primary');
  await ctl.promote('order.get', 'order.get/a2');
  assert.equal(mr.status, 'resolved');
  assert.deepEqual(b.adapters.map(a => a.state), ['deprecated', 'primary']);

  await ctl.runBatch();
  const run = ctl.primary(b).lastRun;
  assert.equal(run.readiness, 'PASS');
  assert.equal(run.stages[0].status, 'pass', 'no false "newer version" warning after migrating');
  assert.match((await ctl.liveCall('order.get'))._meta.served_by, /^v3/);
});

test('a version that is gone with nothing to move to is reported honestly, not hidden', async () => {
  const { ctl, db } = await fresh();
  db.versions.v2 = { rows: [], status: 410 };
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'FAIL');
  assert.equal(openReview(ctl, 'migration').stage, 'no_target');
});

test('a low-confidence migration asks a person which fields are which, then proceeds', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape), retireAt: T0 + 2 * DAY_MS }, v3: { rows: rowsFor(t => ({ ref: t.id, phase: t.status, buyer: t.cust, stamp: t.at })) } });
  await ctl.runBatch();
  const mr = openReview(ctl, 'migration');
  assert.equal(mr.preview.ok, false);
  assert.ok(mr.preview.fieldScores.every(f => Array.isArray(f.candidates)), 'ranked candidates are offered for each field');
  const r1 = await ctl.beginMigration('order.get', 'v3');
  assert.deepEqual([r1.ok, r1.reason], [false, 'needs_mapping']);
  assert.equal(ctl.binding('order.get').adapters.length, 1, 'nothing was guessed or created');
  const r2 = await ctl.beginMigration('order.get', 'v3', { choices: { fields: { order_id: 'ref', status: 'phase', customer_id: 'buyer', created_at: 'stamp' } } });
  assert.equal(r2.ok, true);
  const cand = ctl.binding('order.get').adapters[1];
  assert.equal(cand.lastRun.readiness, 'PASS');
  assert.ok(cand.mapping.fields.every(f => ['ref', 'phase', 'buyer', 'stamp'].includes(f.src)));
});

test('an outage is not drift: no review, no blocked-change count, and it recovers', async () => {
  const { ctl, db } = await fresh();
  await ctl.runBatch();
  db.versions.v2.status = 503; db.versions.v2.error = 'maintenance';
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'FAIL');
  assert.equal(ctl.state.reviews.length, 0);
  assert.equal(ctl.state.metrics.blocked, 0);
  assert.match((await ctl.liveCall('order.get')).error.message, /unavailable \(HTTP 503\)/);
  delete db.versions.v2.status;
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('a transient outage does not roll back a healthy canary', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape), retireAt: T0 + 3 * DAY_MS }, v3: { rows: rowsFor(v3Shape) } });
  await ctl.runBatch(); await ctl.beginMigration('order.get', 'v3'); await ctl.promote('order.get', 'order.get/a2');
  db.versions.v3.status = 503;
  await ctl.runBatch();
  assert.equal(ctl.binding('order.get').adapters[1].state, 'canary');
  delete db.versions.v3.status;
  await ctl.runBatch();
  assert.equal(ctl.binding('order.get').adapters[1].state, 'canary');
  assert.ok(ctl.binding('order.get').adapters[1].canaryPass >= 1);
});

test('a connector that throws, or never answers, cannot take the controller down', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape) } }, { options: { fetchTimeoutMs: 40 } });
  db.throwOnFetch = true;
  await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'FAIL');
  db.throwOnFetch = false; db.hang = true;
  const t0 = Date.now(); await ctl.runBatch();
  assert.ok(Date.now() - t0 < 2000, 'the hang was cut off by the timeout');
  assert.equal(ctl.health('order.get'), 'FAIL');
  db.hang = false; await ctl.runBatch();
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('state-changing operations are serialized even when issued together', async () => {
  const { ctl, db } = await fresh();
  db.delayMs = 8;
  await Promise.all([ctl.runBatch(), ctl.runBatch(), ctl.runBatch()]);
  assert.equal(ctl.state.batches, 3);
  assert.equal(db.maxInflight, 1, 'never two upstream reads at once from competing operations');
});

test('an afterTranslate hook can fail a canary, which then rolls back automatically', async () => {
  const { ctl } = await setup({ v2: { rows: rowsFor(v2Shape), retireAt: T0 + 3 * DAY_MS }, v3: { rows: rowsFor(v3Shape) } });
  let broken = false;
  ctl.hooks.afterTranslate(({ adapter, result }) => (broken && adapter.state === 'canary')
    ? { out: result.out.map((o, i) => i ? o : { ...o, status: 'WRONG' }), errs: result.errs } : undefined);
  await ctl.runBatch(); await ctl.beginMigration('order.get', 'v3'); await ctl.promote('order.get', 'order.get/a2');
  await ctl.runBatch();
  broken = true; await ctl.runBatch();
  assert.equal(ctl.binding('order.get').adapters[1].state, 'rolled_back');
  assert.equal(ctl.primary(ctl.binding('order.get')).upstreamVersion, 'v2', 'the original was never touched');
  assert.equal(openReview(ctl, 'migration').stage, 'rolled_back');
});

test('with no mapping supplied, one is inferred only if every field matches with confidence', async () => {
  const clock = testClock();
  const good = memoryUpstream({ v1: { rows: rowsFor(t => ({ order_id: t.id, status: t.status, customer_id: t.cust, created_at: t.at })) } });
  const ctl = createController({ clock, connectors: { good: good.connector } });
  const ok = await ctl.addBinding({ id: 'orders', connector: 'good', contract: orderContract() });
  assert.equal(ok.ok, true);
  await ctl.runBatch(); assert.equal(ctl.health('orders'), 'PASS');

  const bad = memoryUpstream({ v1: { rows: rowsFor(t => ({ zz: t.id, qq: t.status, yy: t.cust, xx: t.at })) } });
  ctl.registerConnector('bad', bad.connector);
  const no = await ctl.addBinding({ id: 'orders2', connector: 'bad', contract: orderContract() });
  assert.equal(no.ok, false);
  assert.match(no.reason, /Could not infer a mapping with confidence/);
  assert.equal(ctl.binding('orders2'), null, 'nothing is registered on a guess');
});

test('snapshot/restore round-trips the whole controller', async () => {
  const { ctl, connector } = await fresh();
  await ctl.runBatch();
  const snap = ctl.snapshot();
  const other = createController({ clock: testClock(), connectors: { mem: connector } });
  await other.restore(snap);
  assert.equal(other.health('order.get'), 'PASS');
  assert.equal(other.state.batches, 1);
  assert.ok((await other.liveCall('order.get')).data.length > 0);
});

test('two controllers share nothing', async () => {
  const a = await fresh(), b = await fresh();
  await a.ctl.removeBinding('order.get');
  assert.equal(a.ctl.state.bindings.length, 0);
  assert.equal(b.ctl.state.bindings.length, 1);
});

test('bad input is rejected with a reason, not an exception', async () => {
  const { ctl } = await fresh();
  assert.throws(() => ctl.registerConnector('x', {}), TypeError);
  const c = orderContract();
  assert.match((await ctl.addBinding({ id: '', connector: 'mem', contract: c })).reason, /name/);
  assert.match((await ctl.addBinding({ id: '9bad', connector: 'mem', contract: c })).reason, /letters, numbers/);
  assert.match((await ctl.addBinding({ id: 'order.get', connector: 'mem', contract: c })).reason, /already exists/);
  assert.match((await ctl.addBinding({ id: 'ok1', connector: 'nope', contract: c })).reason, /No connector/);
  assert.match((await ctl.addBinding({ id: 'ok2', connector: 'mem' })).reason, /contract/);
});

test('contract deprecation reaches SUNSET on the clock, not on a counter', async () => {
  const { ctl, clock } = await fresh();
  await ctl.deprecateContract('order.get');
  await ctl.runBatch(); assert.equal(ctl.binding('order.get').contract.state, 'DEPRECATED');
  clock.advanceDays(5);
  await ctl.runBatch(); assert.equal(ctl.health('order.get'), 'SUNSET');
  assert.equal((await ctl.liveCall('order.get')).error.code, 'CONTRACT_SUNSET');
});

test('unfamiliar status codes are confirmed by a person too, never guessed', async () => {
  const codes = { in_progress: 'IN_FLIGHT', completed: 'DONE', cancelled: 'VOID' };
  const { ctl } = await setup({ v2: { rows: rowsFor(v2Shape), retireAt: T0 + 2 * DAY_MS }, v3: { rows: rowsFor(t => ({ ref: t.id, phase: codes[t.status], buyer: t.cust, stamp: t.at })) } });
  await ctl.runBatch();
  const fields = { order_id: 'ref', status: 'phase', customer_id: 'buyer', created_at: 'stamp' };
  const half = await ctl.previewMigration('order.get', 'v3', { fields });
  assert.equal(half.ok, false, 'fields chosen, but the codes are still open');
  assert.deepEqual(half.unresolvedValues.map(v => v.value).sort(), ['DONE', 'IN_FLIGHT', 'VOID']);
  assert.equal((await ctl.beginMigration('order.get', 'v3', { choices: { fields } })).reason, 'needs_mapping');
  const values = { 'status:IN_FLIGHT': 'in_progress', 'status:DONE': 'completed', 'status:VOID': 'cancelled' };
  assert.equal((await ctl.previewMigration('order.get', 'v3', { fields, values })).ok, true);
  assert.equal((await ctl.beginMigration('order.get', 'v3', { choices: { fields, values } })).ok, true);
  const cand = ctl.binding('order.get').adapters[1];
  assert.equal(cand.lastRun.readiness, 'PASS', 'candidate output matches the primary on every sampled record');
  assert.equal(await ctl.promote('order.get', cand.id).then(r => r.ok), true);
});

test('a review whose change has gone away closes itself — but not because of an outage', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape) } });
  db.versions.v2.rows = rowsFor(t => ({ orderId: t.id, state: t.status, customerId: t.cust, createdAt: t.at }));
  await ctl.runBatch();
  const rv = openReview(ctl, 'mapping'); assert.ok(rv);
  db.versions.v2.status = 503; await ctl.runBatch();
  assert.equal(rv.status, 'open', 'an outage says nothing about whether the change is still there');
  delete db.versions.v2.status; db.versions.v2.rows = rowsFor(v2Shape);
  await ctl.runBatch();
  assert.equal(rv.status, 'resolved');
  assert.ok(ctl.state.log.some(l => l.action === 'REVIEW_RESOLVED' && l.msg.includes(rv.id)));
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('the same change coming back after its review was approved opens a NEW review (it does not point at the closed one)', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape) } });
  const renamed = rowsFor(t => ({ orderId: t.id, state: t.status, customerId: t.cust, createdAt: t.at }));
  const orig = () => rowsFor(v2Shape), ids = [];
  for (let cycle = 0; cycle < 2; cycle++) {
    db.versions.v2.rows = renamed; await ctl.runBatch();
    const rv = openReview(ctl, 'mapping'); assert.ok(rv, `cycle ${cycle}: a review is open`); ids.push(rv.id);
    assert.equal((await ctl.liveCall('order.get', {})).error.code, 'DRIFT_BLOCKED');
    assert.equal((await ctl.approve(rv.id)).ok, true);
    assert.equal(ctl.health('order.get'), 'PASS');
    db.versions.v2.rows = orig(); await ctl.runBatch();            // it reverts: that is drift too
    const back = openReview(ctl, 'mapping'); assert.ok(back); assert.equal((await ctl.approve(back.id)).ok, true);
  }
  assert.equal(new Set(ids).size, 2, 'the second occurrence got its own review: ' + ids);
  assert.equal(ctl.health('order.get'), 'PASS');
});

test('a rejection sticks while the change stays, and is forgotten once the change has gone', async () => {
  const { ctl, db } = await setup({ v2: { rows: rowsFor(v2Shape) } });
  const renamed = rowsFor(t => ({ orderId: t.id, state: t.status, customerId: t.cust, createdAt: t.at }));
  db.versions.v2.rows = renamed; await ctl.runBatch();
  const r1 = openReview(ctl, 'mapping'); await ctl.reject(r1.id);
  await ctl.runBatch(); await ctl.runBatch();
  assert.equal(ctl.state.reviews.filter(r => r.kind === 'mapping').length, 1, 'no new review each batch: the rejection sticks');
  assert.equal(ctl.health('order.get'), 'REVIEW');
  db.versions.v2.rows = rowsFor(v2Shape); await ctl.runBatch();    // the change goes away
  db.versions.v2.rows = renamed; await ctl.runBatch();             // and comes back
  const r2 = openReview(ctl, 'mapping'); assert.ok(r2 && r2.id !== r1.id, 'a returning change gets a fresh review');
});
