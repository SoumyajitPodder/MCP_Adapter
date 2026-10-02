import { createController } from '../index.js';
import { DAY_MS } from '../utils.js';

export const T0 = Date.parse('2026-10-01T00:00:00Z');
export function testClock() {
  let t = T0;
  return { now: () => t, day: () => Math.floor((t - T0) / DAY_MS), format: ms => new Date(ms).toISOString().slice(0, 10), advanceDays(n) { t += n * DAY_MS; } };
}

export const orderContract = () => ({
  version: '1.2.0',
  fields: [
    { name: 'order_id', type: 'string' },
    { name: 'status', type: 'enum', values: ['in_progress', 'completed', 'cancelled'] },
    { name: 'customer_id', type: 'string' },
    { name: 'created_at', type: 'datetime' }
  ]
});
export const orderMapping = (src = {}) => ({
  version: 1, unwrap: null, sourceTz: 'UTC', coerce: false,
  fields: [
    { target: 'order_id', src: src.order_id || 'orderId', transform: 'identity' },
    { target: 'status', src: src.status || 'orderState', transform: 'enum', values: { in_progress: 'in_progress', completed: 'completed', cancelled: 'cancelled' } },
    { target: 'customer_id', src: src.customer_id || 'customerId', transform: 'identity' },
    { target: 'created_at', src: src.created_at || 'createdAt', transform: 'datetime', format: 'ISO' }
  ]
});

const STATUSES = ['in_progress', 'completed', 'cancelled'];
export const truth = i => ({ id: `ORD-${1000 + i}`, status: STATUSES[i % 3], cust: String(48000 + i * 7), at: `2026-08-0${1 + (i % 8)}T06:00:00Z` });
export const rowsFor = (shape, n = 6) => Array.from({ length: n }, (_, i) => shape(truth(i), i));
export const v2Shape = t => ({ orderId: t.id, orderState: t.status, customerId: t.cust, createdAt: t.at });
export const v3Shape = t => ({ order_id: t.id, order_state: t.status, customer_id: t.cust, created_at: t.at });

// An in-memory upstream you can mutate mid-test. `db.versions[v]` is
// { rows, status?, error?, retireAt? }.
export function memoryUpstream(versions) {
  const db = { versions, inflight: 0, maxInflight: 0, delayMs: 0, throwOnFetch: false, hang: false };
  const connector = {
    async fetch(binding, version, { limit }) {
      db.inflight++; db.maxInflight = Math.max(db.maxInflight, db.inflight);
      try {
        if (db.hang) return await new Promise(() => {});
        if (db.delayMs) await new Promise(r => setTimeout(r, db.delayMs));
        if (db.throwOnFetch) throw new Error('connection reset');
        const v = db.versions[version];
        if (!v) return { status: 404, records: [] };
        if (v.status) return { status: v.status, records: [], error: v.error };
        return { status: 200, records: v.rows.slice(0, limit).map(r => ({ ...r })), retirement: v.retireAt ? { at: v.retireAt } : null };
      } finally { db.inflight--; }
    },
    async listVersions() { return Object.keys(db.versions); }
  };
  return { db, connector };
}

export async function setup(versions, ctlOpts = {}) {
  const clock = testClock();
  const up = memoryUpstream(versions);
  const ctl = createController({ clock, connectors: { mem: up.connector }, ...ctlOpts });
  const r = await ctl.addBinding({ id: 'order.get', displayName: 'Order API', connector: 'mem', contract: orderContract(), mapping: orderMapping(), version: 'v2' }, { quiet: true });
  if (!r.ok) throw new Error('setup failed: ' + r.reason);
  return { ctl, clock, ...up };
}
export const openReview = (ctl, kind) => ctl.state.reviews.find(r => r.status === 'open' && (!kind || r.kind === kind));
