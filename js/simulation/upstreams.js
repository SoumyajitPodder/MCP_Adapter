// Simulated upstream systems: fake REST APIs and a fake file feed that
// start out on a known shape and can be mutated with the drift injectors
// at the bottom of this file. This is the only place in the prototype that
// invents data — every other module only ever sees what fetchUpstream()
// hands back, exactly as a real adapter would only see what a real
// upstream returns.

import { state, CONTRACTS, log } from '../state.js';
import { primary, pad, fmtDate, toggleCase, clone } from '../core/utils.js';

const F = (canon, src, vt = 'string') => ({ canon, src, vt });

// Two upstream shapes per REST binding (its current version, v2, and the
// next version, v3, which only appears once a sunset is simulated), and one
// shape for the file binding.
export const SHAPES = {
  'order.get': {
    v2: {
      envelope: null, dateFormat: 'US', delimiter: ',', colOrder: [], extra: [],
      fields: [F('order_id', 'orderId'), F('status', 'orderState'), F('customer_id', 'customer.id'), F('created_at', 'createdDate')],
      enumMap: { in_progress: 'INPROG', completed: 'COMPLETE', cancelled: 'CANCELLED' }
    },
    v3: {
      envelope: 'data', dateFormat: 'ISO', delimiter: ',', colOrder: [], extra: [],
      fields: [F('order_id', 'order_id'), F('status', 'state'), F('customer_id', 'customerId'), F('created_at', 'created_at')],
      enumMap: { in_progress: 'IN_PROGRESS', completed: 'COMPLETED', cancelled: 'CANCELED' }
    }
  },
  'service.get': {
    v2: {
      envelope: null, dateFormat: 'US', delimiter: ',', colOrder: [], extra: [],
      fields: [F('service_id', 'serviceId'), F('status', 'svcStatus'), F('account_id', 'account.id'), F('activated_at', 'activatedOn')],
      enumMap: { active: 'ACTIVE', suspended: 'SUSPENDED', terminated: 'TERMINATED' }
    },
    v3: {
      envelope: 'result', dateFormat: 'ISO', delimiter: ',', colOrder: [], extra: [],
      fields: [F('service_id', 'service_id'), F('status', 'lifecycle_state'), F('account_id', 'account_ref'), F('activated_at', 'provisioned_at')],
      enumMap: { active: 'LIVE', suspended: 'PAUSED', terminated: 'CLOSED' }
    }
  },
  'inventory.snapshot': {
    v1: {
      envelope: null, dateFormat: 'ISO', delimiter: ',', colOrder: ['item_id', 'status', 'site_id', 'updated_at'], extra: [],
      fields: [F('item_id', 'item_id'), F('status', 'status'), F('site_id', 'site_id'), F('updated_at', 'updated_at')],
      enumMap: { available: 'AVAIL', reserved: 'RSVD', decommissioned: 'DECOM' }
    }
  }
};

// A plain default shape for a newly added API, in whichever style (REST or
// FILE) it was added as. Every existing drift injector assumes four
// canonical fields (an id, a status enum, a secondary identifier, and a
// timestamp), so this matches that shape rather than inventing a new one —
// that's what lets every injector work unmodified on any binding, hand
// authored or added at runtime.
export function makeGenericShape(kind) {
  // Source names are deliberately camelCase and distinct from their
  // canonical snake_case targets (recordId -> id, not id -> id) — a source
  // name that's identical to its target either way you flip its case
  // (like a bare "id" or "status") makes the rename_case injector and the
  // version-migration case-flip both silent no-ops, which is exactly the
  // bug that shipped here the first time.
  const fields = [F('id', 'recordId'), F('status', 'statusCode'), F('ref_id', 'refId'), F('updated_at', 'updatedAt')];
  const enumMap = { active: 'ACTIVE', inactive: 'INACTIVE', pending: 'PENDING' };
  // colOrder is keyed by canonical name, not by source name — cell()
  // looks columns up by canon via `fields`, the same way the hand-authored
  // inventory.snapshot shape does it.
  return kind === 'FILE'
    ? { envelope: null, dateFormat: 'ISO', delimiter: ',', colOrder: ['id', 'status', 'ref_id', 'updated_at'], extra: [], fields, enumMap }
    : { envelope: null, dateFormat: 'ISO', delimiter: ',', colOrder: [], extra: [], fields, enumMap };
}

// The name the version after `v` would have, so a graceful sunset or an
// overnight cutover can be simulated on any binding, not just the two with
// a hand-authored v3 already waiting for them.
function nextVersionName(v) {
  const m = /^v(\d+)$/.exec(v);
  return m ? 'v' + (parseInt(m[1], 10) + 1) : v + '-next';
}

// When no hand-authored next-version shape exists for this binding (true
// for every binding added at runtime, and for inventory.snapshot which
// never gets a version-drift injector), derive a plausible one instead of
// refusing to simulate a migration at all: flip the case of every field
// name and the envelope, leaving the meaning of each field and its codes
// untouched. That's enough for the automatic mapping proposer to find a
// confident match on every field, which is the point — a new API should
// be able to demonstrate a clean version migration immediately, without
// someone hand-authoring a second shape for it first.
function deriveNextShape(shape) {
  return {
    envelope: shape.envelope ? null : 'data',
    dateFormat: shape.dateFormat === 'US' ? 'ISO' : 'US',
    delimiter: shape.delimiter,
    colOrder: shape.colOrder.slice(),
    extra: shape.extra.slice(),
    fields: shape.fields.map(f => ({ canon: f.canon, src: toggleCase(f.src), vt: f.vt })),
    enumMap: Object.assign({}, shape.enumMap)
  };
}

/* ---- ground truth + rendering ----
   "truth" is what the upstream really holds, independent of its current
   shape. Rendering (renderRest / renderFile) is what turns that truth into
   the upstream's current on-the-wire representation, so injecting drift
   only ever changes how truth is rendered, never the truth itself. */

function truth(b, i, isNew, isSilent) {
  return {
    i,
    key: b.prefix + (1000 + i),
    other: String(48000 + i * 7),
    created: new Date(Date.UTC(2026, 7, 1 + (i % 25), 6 + (i % 12), (i * 7) % 60, (i * 13) % 60)),
    status: isNew ? b.newState.truth : CONTRACTS[b.tool].fields[1].values[i % 3],
    silent: !!isSilent
  };
}

function sampleTruths(b, shape, n) {
  const out = [];
  const extra = (b.newActive ? 2 : 0) + (b.silentActive ? 2 : 0);
  for (let i = 0; out.length < n - extra && i < 300; i++) {
    const t = truth(b, i, false, false);
    if (shape.enumMap[t.status] !== undefined) out.push(t);
  }
  if (b.newActive) {
    [100, 101].forEach(i => { const t = truth(b, i, true, false); if (shape.enumMap[t.status] !== undefined) out.push(t); });
  }
  if (b.silentActive) {
    // Brand-new records the adapter has never seen before, so there is no
    // last-known-good entry to catch them against. Their true status is a
    // perfectly ordinary one, but the wire reports it using a different,
    // already-known code — the silent-swap scenario, isolated to records
    // with no history, which is what actually makes it undetectable.
    [150, 151].forEach(i => { const t = truth(b, i, false, true); out.push(t); });
  }
  return out;
}

function cell(b, shape, f, t) {
  const idx = b.canon.indexOf(f.canon);
  if (idx === 0) return t.key;
  if (idx === 1) {
    if (t.silent) {
      const keys = Object.keys(shape.enumMap);
      const swapped = keys.find(k => k !== t.status) || t.status;
      return shape.enumMap[swapped];
    }
    return shape.enumMap[t.status];
  }
  if (idx === 2) return f.vt === 'number' ? Number(t.other) : t.other;
  return fmtDate(t.created, shape.dateFormat);
}

function setPath(o, p, v) {
  const s = p.split('.');
  let c = o;
  s.slice(0, -1).forEach(k => c = c[k] = c[k] || {});
  c[s[s.length - 1]] = v;
}

function renderRest(b, shape, ts) {
  return ts.map(t => {
    const o = {};
    shape.fields.forEach(f => setPath(o, f.src, cell(b, shape, f, t)));
    shape.extra.forEach(x => o[x] = 'web');
    return shape.envelope ? { [shape.envelope]: o } : o;
  });
}

function renderFile(b, shape, ts) {
  const cols = shape.colOrder.map(c => shape.fields.find(f => f.canon === c)).filter(Boolean);
  const header = cols.map(f => f.src).concat(shape.extra);
  const rows = ts.map(t => cols.map(f => cell(b, shape, f, t)).concat(shape.extra.map(() => 'web')).join(shape.delimiter));
  return [header.join(shape.delimiter)].concat(rows).join('\n');
}

// The one function every other module calls to "hit" an upstream. Returns
// an HTTP-shaped result (status, headers, body) exactly like a real
// integration would give an adapter. `sunsetVersion` (set by the sunset /
// version_bump injectors below, not hardcoded here) is whichever version
// was primary at the moment a retirement was announced — so this works
// the same way regardless of what that version happens to be named.
export function fetchUpstream(b, ver, n = 5) {
  const u = b.upstream;
  const retiring = b.kind === 'REST' && u.sunsetDay != null && ver === u.sunsetVersion;
  if (retiring && state.day >= u.sunsetDay) {
    return { status: 410, headers: {}, ver, truths: [] };
  }
  const shape = u.versions[ver];
  if (!shape) return { status: 404, headers: {}, ver, truths: [] };
  const ts = sampleTruths(b, shape, n);
  const headers = {};
  if (retiring) headers.Sunset = 'day ' + u.sunsetDay;
  return b.kind === 'FILE'
    ? { status: 200, headers, ver, truths: ts, text: renderFile(b, shape, ts) }
    : { status: 200, headers, ver, truths: ts, items: renderRest(b, shape, ts) };
}

/* ---- drift injection ----
   Each injector mutates the *shape* the primary adapter's upstream version
   currently renders with. The next fetchUpstream() call (and so the next
   batch or live call) will reflect the change, and the detector picks it
   up the same way it would pick up a real upstream change. */

function targetShape(b) {
  const a = primary(b);
  return b.upstream.versions[a.upstreamVersion];
}

export const INJ = [
  { id: 'add_optional', label: 'Add an optional field', kinds: ['REST', 'FILE'],
    apply: (b, s) => { if (!s.extra.includes('channel')) s.extra.push('channel'); } },
  { id: 'rename_case', label: 'Rename first field (camelCase ↔ snake_case)', kinds: ['REST', 'FILE'],
    apply: (b, s) => { const f = s.fields[0]; f.src = toggleCase(f.src); } },
  { id: 'rename_status', label: 'Rename the status field', kinds: ['REST', 'FILE'],
    apply: (b, s) => { s.fields.find(f => f.canon === b.canon[1]).src = b.altStatus; } },
  { id: 'type_change', label: 'Second identifier becomes a number', kinds: ['REST', 'FILE'],
    apply: (b, s) => { s.fields.find(f => f.canon === b.canon[2]).vt = 'number'; } },
  { id: 'new_enum', label: 'Introduce a new status value', kinds: ['REST', 'FILE'],
    apply: (b, s) => { b.newActive = true; s.enumMap[b.newState.truth] = b.newState.up; } },
  { id: 'date_format', label: 'Change the timestamp format', kinds: ['REST', 'FILE'],
    apply: (b, s) => { s.dateFormat = s.dateFormat === 'US' ? 'ISO' : 'US'; } },
  { id: 'envelope', label: 'Wrap the response in an envelope', kinds: ['REST'],
    apply: (b, s) => { s.envelope = s.envelope || 'data'; } },
  { id: 'reorder', label: 'Reorder the columns', kinds: ['FILE'],
    apply: (b, s) => { s.colOrder.reverse(); } },
  { id: 'remove_required', label: 'Remove a required field', kinds: ['REST', 'FILE'],
    apply: (b, s) => { s.fields = s.fields.filter(f => f.canon !== b.canon[3]); s.colOrder = s.colOrder.filter(c => c !== b.canon[3]); } },
  { id: 'delimiter', label: 'Change the file delimiter', kinds: ['FILE'],
    apply: (b, s) => { s.delimiter = '|'; } },
  { id: 'sunset', label: 'Announce current version sunset in 6 days, release the next version', kinds: ['REST'], custom: true,
    customLog: 'upstream announced a sunset (6 days) and released the next version',
    apply: (b) => {
      if (b.upstream.sunsetDay != null) return;
      const cur = primary(b).upstreamVersion, next = nextVersionName(cur);
      if (b.upstream.versions[next]) return;
      const nextShape = (SHAPES[b.tool] && SHAPES[b.tool][next]) ? clone(SHAPES[b.tool][next]) : deriveNextShape(b.upstream.versions[cur]);
      b.upstream.sunsetVersion = cur;
      b.upstream.sunsetDay = state.day + 6;
      b.upstream.versions[next] = nextShape;
      b.pristine[next] = clone(nextShape);
    } },
  // Distinct from the graceful sunset above: the vendor cuts the old
  // endpoint over immediately, with no advance notice at all. The primary
  // sees a 410 on its very next call, with no prior REVIEW-level warning —
  // this exercises the unplanned/BREAKING path instead of the planned one.
  { id: 'version_bump', label: 'API endpoint changed overnight (current version gone, no warning)', kinds: ['REST'], custom: true,
    customLog: 'upstream cut over to a new API version overnight — the current version is gone effective immediately, no warning header was ever shown',
    apply: (b) => {
      if (b.upstream.sunsetDay != null) return;
      const cur = primary(b).upstreamVersion, next = nextVersionName(cur);
      if (b.upstream.versions[next]) return;
      const nextShape = (SHAPES[b.tool] && SHAPES[b.tool][next]) ? clone(SHAPES[b.tool][next]) : deriveNextShape(b.upstream.versions[cur]);
      b.upstream.sunsetVersion = cur;
      b.upstream.sunsetDay = state.day;
      b.upstream.versions[next] = nextShape;
      b.pristine[next] = clone(nextShape);
    } },
  // The dangerous case the LLD calls out separately from ordinary drift:
  // brand-new records report a perfectly ordinary status using an
  // already-known code, just the wrong one. Nothing about the schema
  // changes, no new code appears, and there is no last-known-good entry
  // for these records to catch the mismatch against — this is the
  // genuine blind spot, not just something the field classifier misses.
  { id: 'silent_swap', label: 'Silent breaking change (new records, meaning swapped)', kinds: ['REST', 'FILE'],
    note: 'brand-new records only, already-known codes — nothing in the pipeline flags this',
    apply: (b) => { b.silentActive = true; } }
];

export function inject(b, id) {
  const inj = INJ.find(x => x.id === id);
  if (!inj) return;
  if (inj.custom) {
    inj.apply(b);
    log(b, 'SYSTEM', 'simulated: ' + (inj.customLog || inj.label.toLowerCase()), 'sim', { actor: 'simulator', action: 'SIMULATION' });
    return;
  }
  inj.apply(b, targetShape(b));
  log(b, 'SYSTEM', 'simulated: ' + inj.label.toLowerCase() + (inj.note ? ` — ${inj.note}` : ''), 'sim', { actor: 'simulator', action: 'SIMULATION' });
}
