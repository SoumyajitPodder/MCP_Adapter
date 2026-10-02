// The simulation: fake upstream systems, a simulated clock, drift injectors,
// a drift queue, and scripted scenarios — everything that exists only for
// the demo. It is built ENTIRELY on the controller's public interface:
//
//   - it is a connector (registered as "simulation"), exactly like the HTTP,
//     file and SQL connectors, so the controller cannot tell it from a real one;
//   - it supplies the controller's clock;
//   - it registers an afterTranslate hook to inject canary faults;
//   - it writes to the audit trail through controller.audit().
//
// Nothing in controller/ imports this file. Delete this folder and the
// controller still works against any real upstream.

import { flatten, primary, toggleCase, fmtDate, clone, versionLabel, DAY_MS } from '../controller/utils.js';
import { parseDelimited } from '../controller/connectors/helpers.js';
import { SCENARIOS } from '../data/scenarios.js';

const F = (canon, src, vt = 'string') => ({ canon, src, vt });

// Two upstream shapes per hand-authored REST binding (its current version,
// v2, and the next, v3, which only appears once a sunset is simulated), and
// one shape for the file binding.
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

// A plain default shape and contract for an API added from the UI. Every
// injector assumes four canonical fields (an id, a status enum, a secondary
// identifier, a timestamp), so a generated API matches that shape — which is
// what lets every injector work on it unmodified.
export function makeGenericShape(kind) {
  // Source names are deliberately camelCase and distinct from their
  // canonical snake_case targets: a name identical either way you flip its
  // case (a bare "id") would make the rename injector a silent no-op.
  const fields = [F('id', 'recordId'), F('status', 'statusCode'), F('ref_id', 'refId'), F('updated_at', 'updatedAt')];
  const enumMap = { active: 'ACTIVE', inactive: 'INACTIVE', pending: 'PENDING' };
  // colOrder is keyed by canonical name, like the hand-authored file shape.
  return kind === 'FILE'
    ? { envelope: null, dateFormat: 'ISO', delimiter: ',', colOrder: ['id', 'status', 'ref_id', 'updated_at'], extra: [], fields, enumMap }
    : { envelope: null, dateFormat: 'ISO', delimiter: ',', colOrder: [], extra: [], fields, enumMap };
}
export function genericContract() {
  return {
    version: '1.0.0', state: 'ACTIVE', sunsetAt: null,
    fields: [
      { name: 'id', type: 'string' },
      { name: 'status', type: 'enum', values: ['active', 'inactive', 'pending'] },
      { name: 'ref_id', type: 'string' },
      { name: 'updated_at', type: 'datetime' }
    ]
  };
}

function nextVersionName(v) {
  const m = /^v(\d+)$/.exec(v);
  return m ? 'v' + (parseInt(m[1], 10) + 1) : v + '-next';
}
// When no hand-authored next-version shape exists, derive a plausible one:
// flip the case of every field name and the envelope, leaving meaning and
// codes untouched, so the automatic proposer can match every field.
function deriveNextShape(shape) {
  return {
    envelope: shape.envelope ? null : 'data',
    dateFormat: shape.dateFormat === 'US' ? 'ISO' : 'US',
    delimiter: shape.delimiter, colOrder: shape.colOrder.slice(), extra: shape.extra.slice(),
    fields: shape.fields.map(f => ({ canon: f.canon, src: toggleCase(f.src), vt: f.vt })),
    enumMap: Object.assign({}, shape.enumMap)
  };
}

// A straightforward 1:1 mapping from a simulated shape to the contract's
// fields. This is the sim playing the part of "a person who wrote the
// mapping by hand" — it is passed to the controller as input, never read by it.
function deriveMapping(contract, shape) {
  const fields = [];
  for (const cf of contract.fields) {
    const f = shape.fields.find(x => x.canon === cf.name);
    if (!f) return null;
    const m = { target: cf.name, src: f.src };
    if (cf.type === 'string') m.transform = 'identity';
    if (cf.type === 'enum') { m.transform = 'enum'; m.values = Object.fromEntries(Object.entries(shape.enumMap).map(([k, v]) => [v, k])); }
    if (cf.type === 'datetime') { m.transform = 'datetime'; m.format = shape.dateFormat; }
    fields.push(m);
  }
  return { version: 1, unwrap: shape.envelope, sourceTz: 'UTC', coerce: false, fields };
}

function setPath(o, p, v) {
  const s = p.split('.');
  let c = o;
  s.slice(0, -1).forEach(k => { c = c[k] = c[k] || {}; });
  c[s[s.length - 1]] = v;
}

// The simulated clock. The controller only ever sees now()/day()/format().
export function createSimClock() {
  const state = { day: 0, clockMinutes: 0 };
  return {
    state,
    now: () => state.day * DAY_MS,
    day: () => state.day,
    format: ms => 'day ' + Math.round(ms / DAY_MS),
    reset() { state.day = 0; state.clockMinutes = 0; }
  };
}

export function createSimulation({ controller, clock }) {
  const state = { scenarioQueue: [], qseq: 0, activeScenarios: [] };
  const upstreams = {};      // binding id -> what that fake upstream really holds
  const faults = new Set();  // adapter ids carrying an injected canary fault
  const asB = x => (typeof x === 'string' ? controller.binding(x) : x);
  const say = (b, msg, actor = 'simulator', action = 'SIMULATION') => controller.audit(b, 'SYSTEM', msg, 'sim', { actor, action });

  /* ---- ground truth + rendering ----
     "truth" is what the upstream really holds, independent of its current
     shape. Rendering turns that truth into the upstream's current wire
     representation, so injecting drift only ever changes how truth is
     rendered, never the truth itself. */

  function truth(b, u, i, isNew, isSilent) {
    return {
      i, key: u.prefix + (1000 + i), other: String(48000 + i * 7),
      created: new Date(Date.UTC(2026, 7, 1 + (i % 25), 6 + (i % 12), (i * 7) % 60, (i * 13) % 60)),
      status: isNew ? u.newState.truth : b.contract.fields[1].values[i % 3],
      silent: !!isSilent
    };
  }
  function sampleTruths(b, u, shape, n) {
    const out = [];
    const extra = (u.newActive ? 2 : 0) + (u.silentActive ? 2 : 0);
    for (let i = 0; out.length < n - extra && i < 300; i++) {
      const t = truth(b, u, i, false, false);
      if (shape.enumMap[t.status] !== undefined) out.push(t);
    }
    if (u.newActive) [100, 101].forEach(i => { const t = truth(b, u, i, true, false); if (shape.enumMap[t.status] !== undefined) out.push(t); });
    // Brand-new records the adapter has never seen, so there is no
    // last-known-good entry to catch them against: their true status is
    // ordinary, but the wire reports it with a different already-known code.
    if (u.silentActive) [150, 151].forEach(i => out.push(truth(b, u, i, false, true)));
    return out;
  }
  function cell(b, shape, f, t) {
    const idx = b.canon.indexOf(f.canon);
    if (idx === 0) return t.key;
    if (idx === 1) {
      if (t.silent) { const swapped = Object.keys(shape.enumMap).find(k => k !== t.status) || t.status; return shape.enumMap[swapped]; }
      return shape.enumMap[t.status];
    }
    if (idx === 2) return f.vt === 'number' ? Number(t.other) : t.other;
    return fmtDate(t.created, shape.dateFormat);
  }
  const renderRest = (b, shape, ts) => ts.map(t => {
    const o = {};
    shape.fields.forEach(f => setPath(o, f.src, cell(b, shape, f, t)));
    shape.extra.forEach(x => { o[x] = 'web'; });
    return shape.envelope ? { [shape.envelope]: o } : o;
  });
  function renderFile(b, shape, ts) {
    const cols = shape.colOrder.map(c => shape.fields.find(f => f.canon === c)).filter(Boolean);
    const header = cols.map(f => f.src).concat(shape.extra);
    const rows = ts.map(t => cols.map(f => cell(b, shape, f, t)).concat(shape.extra.map(() => 'web')).join(shape.delimiter));
    return [header.join(shape.delimiter)].concat(rows).join('\n');
  }

  /* ---- the connector ---- */

  const connector = {
    async fetch(binding, version, { limit = 5, hints } = {}) {
      const u = upstreams[binding.id];
      if (!u) return { status: 404, records: [], error: 'unknown simulated upstream' };
      const retiring = u.kind === 'REST' && u.sunsetDay != null && version === u.sunsetVersion;
      if (retiring && clock.day() >= u.sunsetDay) return { status: 410, records: [], error: 'version retired' };
      const shape = u.versions[version];
      if (!shape) return { status: 404, records: [], error: 'no such version' };
      const ts = sampleTruths(binding, u, shape, limit);
      const headers = retiring ? { Sunset: 'day ' + u.sunsetDay } : {};
      const retirement = retiring ? { at: u.sunsetDay * DAY_MS } : null;
      if (u.kind === 'FILE') {
        // Parse with the delimiter the baseline was captured with, not the
        // one the file uses today: that is how a change gets noticed.
        const p = parseDelimited(renderFile(binding, shape, ts), (hints && hints.delimiter) || shape.delimiter || ',');
        return p.unreadable
          ? { status: 200, headers, retirement, records: [], unreadable: true, format: p.format }
          : { status: 200, headers, retirement, records: p.records, columns: p.columns, format: p.format };
      }
      return { status: 200, headers, retirement, records: renderRest(binding, shape, ts).map(x => flatten(x)) };
    },
    async listVersions(binding) { const u = upstreams[binding.id]; return u ? Object.keys(u.versions) : []; },
    async operatorMapping(binding, version) { const u = upstreams[binding.id]; return u && u.versions[version] ? deriveMapping(binding.contract, u.versions[version]) : null; }
  };

  /* ---- drift injection ----
     Each injector mutates the shape the primary adapter's upstream version
     currently renders with. The next fetch reflects it, and the detector
     picks it up the way it would pick up a real upstream change. `x` is
     { b: binding, u: this binding's simulated upstream }. */

  const retirementPending = b => {
    const u = upstreams[b.id], p = primary(b);
    return !!u && u.sunsetDay != null && !!p && u.sunsetVersion === p.upstreamVersion;
  };
  function release(x, inDays) {
    if (retirementPending(x.b)) return;
    const cur = primary(x.b).upstreamVersion, next = nextVersionName(cur), u = x.u;
    if (u.versions[next]) return;
    const authored = SHAPES[x.b.tool] && SHAPES[x.b.tool][next];
    u.sunsetVersion = cur; u.sunsetDay = clock.day() + inDays;
    u.versions[next] = authored ? clone(authored) : deriveNextShape(u.versions[cur]);
  }

  const INJ = [
    { id: 'add_optional', label: 'Add an optional field', kinds: ['REST', 'FILE'], apply: (x, s) => { if (!s.extra.includes('channel')) s.extra.push('channel'); } },
    { id: 'rename_case', label: 'Rename first field (camelCase ↔ snake_case)', kinds: ['REST', 'FILE'], apply: (x, s) => { const f = s.fields[0]; f.src = toggleCase(f.src); } },
    { id: 'rename_status', label: 'Rename the status field', kinds: ['REST', 'FILE'], apply: (x, s) => { s.fields.find(f => f.canon === x.b.canon[1]).src = x.u.altStatus; } },
    { id: 'type_change', label: 'Second identifier becomes a number', kinds: ['REST', 'FILE'], apply: (x, s) => { s.fields.find(f => f.canon === x.b.canon[2]).vt = 'number'; } },
    { id: 'new_enum', label: 'Introduce a new status value', kinds: ['REST', 'FILE'], apply: (x, s) => { x.u.newActive = true; s.enumMap[x.u.newState.truth] = x.u.newState.up; } },
    { id: 'date_format', label: 'Change the timestamp format', kinds: ['REST', 'FILE'], apply: (x, s) => { s.dateFormat = s.dateFormat === 'US' ? 'ISO' : 'US'; } },
    { id: 'envelope', label: 'Wrap the response in an envelope', kinds: ['REST'], apply: (x, s) => { s.envelope = s.envelope || 'data'; } },
    { id: 'reorder', label: 'Reorder the columns', kinds: ['FILE'], apply: (x, s) => { s.colOrder.reverse(); } },
    { id: 'remove_required', label: 'Remove a required field', kinds: ['REST', 'FILE'], apply: (x, s) => { s.fields = s.fields.filter(f => f.canon !== x.b.canon[3]); s.colOrder = s.colOrder.filter(c => c !== x.b.canon[3]); } },
    { id: 'delimiter', label: 'Change the file delimiter', kinds: ['FILE'], apply: (x, s) => { s.delimiter = '|'; } },
    { id: 'sunset', label: 'Announce current version sunset in 6 days, release the next version', kinds: ['REST'], custom: true,
      customLog: 'upstream announced a sunset (6 days) and released the next version', apply: x => release(x, 6) },
    // Distinct from the graceful sunset: the vendor cuts the old endpoint
    // over immediately with no notice, so the primary sees a 410 on its very
    // next call — the unplanned/BREAKING path instead of the planned one.
    { id: 'version_bump', label: 'API endpoint changed overnight (current version gone, no warning)', kinds: ['REST'], custom: true,
      customLog: 'upstream cut over to a new API version overnight — the current version is gone effective immediately, no warning header was ever shown', apply: x => release(x, 0) },
    // The dangerous case: brand-new records report an ordinary status using
    // an already-known code, just the wrong one. No schema change, no new
    // code, and no last-known-good entry to catch it against.
    { id: 'silent_swap', label: 'Silent breaking change (new records, meaning swapped)', kinds: ['REST', 'FILE'],
      note: 'brand-new records only, already-known codes — nothing in the pipeline flags this', apply: x => { x.u.silentActive = true; } }
  ];

  function inject(x, id) {
    const b = asB(x), u = upstreams[b.id], inj = INJ.find(q => q.id === id);
    if (!inj || !u) return;
    if (inj.custom) { inj.apply({ b, u }); say(b, 'simulated: ' + (inj.customLog || inj.label.toLowerCase())); return; }
    inj.apply({ b, u }, u.versions[primary(b).upstreamVersion]);
    say(b, 'simulated: ' + inj.label.toLowerCase() + (inj.note ? ` — ${inj.note}` : ''));
  }

  /* ---- the drift queue ----
     Both manual injections and scenario steps go through it, so there is one
     place to see what is about to happen to an upstream and one place to
     call it off. Each entry lands at the start of a batch once its day has
     arrived: manual entries are due immediately, scenario entries on their
     scheduled day. */

  function enqueueDrift(entry) { state.qseq++; state.scenarioQueue.push(Object.assign({ id: 'Q' + state.qseq }, entry)); }

  function queueDrift(x, injectId) {
    const b = asB(x), inj = INJ.find(q => q.id === injectId);
    if (!b || !inj) return;
    if (state.scenarioQueue.some(q => q.bindingId === b.id && q.injectId === injectId)) return;
    enqueueDrift({ day: clock.day(), bindingId: b.id, injectId, note: inj.label, scenarioId: null, scenarioName: 'Manual' });
    say(b, `queued drift: ${inj.label} — lands at the next batch run`, 'operator');
  }

  // A scenario already queued is left alone, and so is a scenario for a
  // binding another scenario is already working on — two scripted stories on
  // one upstream would just tangle together.
  function activateScenario(scenarioId) {
    const sc = SCENARIOS.find(q => q.id === scenarioId);
    if (!sc || state.activeScenarios.includes(scenarioId)) return;
    if (SCENARIOS.some(q => state.activeScenarios.includes(q.id) && q.bindingId === sc.bindingId)) return;
    state.activeScenarios.push(scenarioId);
    sc.steps.forEach(step => enqueueDrift({ day: clock.day() + step.offset, bindingId: sc.bindingId, injectId: step.injectId, note: step.note, scenarioId, scenarioName: sc.name }));
    say(null, `scenario "${sc.name}" added on ${sc.bindingId} — ${sc.steps.length} event${sc.steps.length === 1 ? '' : 's'} queued`, 'operator');
  }

  function cancelQueuedDrift(queueId) {
    const item = state.scenarioQueue.find(q => q.id === queueId);
    if (!item) return;
    state.scenarioQueue = state.scenarioQueue.filter(q => q.id !== queueId);
    if (item.scenarioId && !state.scenarioQueue.some(q => q.scenarioId === item.scenarioId)) state.activeScenarios = state.activeScenarios.filter(id => id !== item.scenarioId);
    say(controller.binding(item.bindingId), `cancelled queued drift: ${item.note} (${item.scenarioName}, was due day ${item.day})`, 'operator', 'SIMULATION_CANCELLED');
  }

  // Lands every queued injection whose day has arrived, then frees any
  // scenario whose steps have all fired.
  function fireDue() {
    const due = state.scenarioQueue.filter(s => s.day <= clock.day());
    if (!due.length) return;
    state.scenarioQueue = state.scenarioQueue.filter(s => s.day > clock.day());
    due.forEach(s => {
      const b = controller.binding(s.bindingId);
      if (b) inject(b, s.injectId);
      if (s.scenarioId && !state.scenarioQueue.some(q => q.scenarioId === s.scenarioId)) state.activeScenarios = state.activeScenarios.filter(id => id !== s.scenarioId);
    });
  }

  // What the UI calls before every controller batch: optionally move to the
  // next day, then land whatever upstream changes are due.
  function beforeBatch(advance) {
    if (advance) { clock.state.day++; clock.state.clockMinutes = 0; }
    fireDue();
  }

  /* ---- canary faults ----
     Simulates "the new adapter is worse": a candidate that passed the
     sandbox can still carry a correctness bug that only shows up under
     continued running. After translation one record's status is corrupted,
     so canonical validation and the last-known-good regression catch it in
     stage 5 — exactly as they would catch a real mapping bug. */

  const applyCanaryFault = (b, t) => t.out.length
    ? { out: t.out.map((o, i) => i === 0 ? Object.assign({}, o, { [b.canon[1]]: 'WRONG_' + o[b.canon[1]] }) : o), errs: t.errs }
    : t;

  function injectCanaryFault(x, y) {
    const b = asB(x), a = b && b.adapters.find(q => q.id === (typeof y === 'string' ? y : y.id));
    if (!a || a.state !== 'canary' || faults.has(a.id)) return;
    faults.add(a.id);
    say(b, `simulated: injected a correctness bug into ${versionLabel(b, a)} (canary) — will surface on the next run`);
  }

  /* ---- registering simulated APIs ---- */

  // `def` is the hand-authored binding definition (data/bindings.js) plus its
  // contract. The sim keeps what the fake upstream holds; the controller gets
  // only the generic definition and a mapping, like any other binding.
  async function addBinding(def, opts) {
    if (controller.binding(def.id)) return { ok: false, reason: `An API called "${def.id}" already exists.` };
    const shape = (SHAPES[def.tool] && SHAPES[def.tool][def.ver]) || def.shape;
    upstreams[def.id] = { id: def.id, kind: def.kind, prefix: def.prefix, altStatus: def.altStatus, newState: def.newState, versions: { [def.ver]: clone(shape) }, sunsetDay: null, sunsetVersion: null, newActive: false, silentActive: false };
    const r = await controller.addBinding({
      id: def.id, tool: def.tool, displayName: def.displayName, kind: def.kind, system: def.system, iface: def.iface,
      connector: 'simulation', contract: def.contract, version: def.ver, mapping: deriveMapping(def.contract, shape)
    }, opts);
    if (!r.ok) delete upstreams[def.id];
    return r;
  }

  // What the "Add an API" form does: a generic REST or file-feed API.
  function addGenericBinding({ id, displayName, kind }) {
    kind = kind === 'FILE' ? 'FILE' : 'REST';
    return addBinding({
      id, tool: id, displayName: displayName || id, kind, system: 'custom', iface: kind === 'FILE' ? `${id}.csv` : `GET /${id}`,
      prefix: (String(id).replace(/[^a-z0-9]/gi, '').slice(0, 3) || 'API').toUpperCase() + '-', altStatus: 'state',
      newState: { truth: 'archived', up: 'ARCHIVED' }, ver: 'v1', contract: genericContract(), shape: makeGenericShape(kind)
    });
  }

  function install() {
    controller.registerConnector('simulation', connector);
    controller.hooks.afterTranslate(({ binding, adapter, result }) => (faults.has(adapter.id) ? applyCanaryFault(binding, result) : undefined));
    controller.on('binding.removed', ({ id }) => { delete upstreams[id]; state.scenarioQueue = state.scenarioQueue.filter(q => q.bindingId !== id); });
  }

  function reset() {
    Object.assign(state, { scenarioQueue: [], qseq: 0, activeScenarios: [] });
    faults.clear();
    Object.keys(upstreams).forEach(k => delete upstreams[k]);
    clock.reset();
  }

  return {
    state, connector, INJ, install, reset, addBinding, addGenericBinding,
    inject, queueDrift, activateScenario, cancelQueuedDrift, fireDue, beforeBatch,
    injectCanaryFault, hasFault: id => faults.has(id), retirementPending,
    peek: id => upstreams[id] // what a fake upstream really holds — for tests and devtools only
  };
}
