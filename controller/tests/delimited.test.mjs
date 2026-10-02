import test from 'node:test';
import assert from 'node:assert/strict';
import { createController, createDelimitedFileConnector, parseDelimited } from '../index.js';
import { testClock, openReview } from './helpers.mjs';

const contract = () => ({ version: '1.0.0', fields: [
  { name: 'item_id', type: 'string' }, { name: 'status', type: 'enum', values: ['available', 'reserved', 'decommissioned'] },
  { name: 'site_id', type: 'string' }, { name: 'updated_at', type: 'datetime' }] });
const mapping = () => ({ version: 1, unwrap: null, sourceTz: 'UTC', coerce: false, fields: [
  { target: 'item_id', src: 'item_id', transform: 'identity' },
  { target: 'status', src: 'status', transform: 'enum', values: { AVAIL: 'available', RSVD: 'reserved', DECOM: 'decommissioned' } },
  { target: 'site_id', src: 'site_id', transform: 'identity' },
  { target: 'updated_at', src: 'updated_at', transform: 'datetime', format: 'ISO' }] });
const CSV = (d = ',', cols = ['item_id', 'status', 'site_id', 'updated_at']) => {
  const alias = { stock_state: 'status' }; // a renamed header still carries the same data
  const row = { item_id: i => `ITM-${1000 + i}`, status: i => ['AVAIL', 'RSVD', 'DECOM'][i % 3], site_id: i => `S${i}`, updated_at: () => '2026-08-01T06:00:00Z' };
  return [cols.join(d), ...[0, 1, 2, 3, 4, 5].map(i => cols.map(c => row[alias[c] || c](i)).join(d))].join('\n');
};

async function boot() {
  const files = { '/feed.csv': CSV() };
  const readText = async p => { if (!(p in files)) { const e = new Error(`ENOENT: ${p}`); e.code = 'ENOENT'; throw e; } return files[p]; };
  const ctl = createController({ clock: testClock(), connectors: { csv: createDelimitedFileConnector({ readText }) } });
  const r = await ctl.addBinding({ id: 'inventory', connector: 'csv', source: { path: '/feed.csv' }, contract: contract(), mapping: mapping() }, { quiet: true });
  assert.equal(r.ok, true, r.reason);
  return { files, ctl };
}

test('csv: quoted fields and escaped quotes parse correctly', () => {
  const p = parseDelimited('a,b,c\n1,"x, y",3\n4,"say ""hi""",6');
  assert.deepEqual(p.records, [{ a: '1', b: 'x, y', c: '3' }, { a: '4', b: 'say "hi"', c: '6' }]);
  assert.equal(parseDelimited('only-one-column\n1').unreadable, true);
});

test('csv: a reordered file is harmless; a renamed header goes to review', async () => {
  const { files, ctl } = await boot();
  await ctl.runBatch(); assert.equal(ctl.health('inventory'), 'PASS');
  files['/feed.csv'] = CSV(',', ['updated_at', 'site_id', 'status', 'item_id']);
  await ctl.runBatch(); assert.equal(ctl.health('inventory'), 'PASS');
  assert.ok(ctl.state.log.some(l => /columns reordered/.test(l.msg)));
  files['/feed.csv'] = CSV(',', ['item_id', 'stock_state', 'site_id', 'updated_at']);
  await ctl.runBatch();
  const rv = openReview(ctl, 'mapping');
  assert.ok(rv && rv.renames.some(r => r.to === 'stock_state'));
  await ctl.approve(rv.id); assert.equal(ctl.health('inventory'), 'PASS');
});

test('csv: a changed delimiter is reported, not silently re-parsed', async () => {
  const { files, ctl } = await boot();
  files['/feed.csv'] = CSV('|');
  await ctl.runBatch();
  assert.equal(ctl.health('inventory'), 'FAIL');
  assert.ok(ctl.state.log.some(l => /format unreadable/.test(l.msg)));
  assert.equal((await ctl.liveCall('inventory')).error.code, 'DRIFT_BLOCKED');
});

test('csv: a missing file is an outage, not a migration', async () => {
  const { files, ctl } = await boot();
  delete files['/feed.csv'];
  await ctl.runBatch();
  assert.equal(ctl.health('inventory'), 'FAIL'); assert.equal(ctl.state.reviews.length, 0);
});
