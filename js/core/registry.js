// Adding or removing an API at runtime. This is what turns "three fixed
// bindings baked in at startup" into something that can grow — a new API
// gets a plain, generic contract and upstream shape so every existing
// drift injector and the whole detect/classify/validate/lifecycle pipeline
// works on it immediately, with no special-casing anywhere else.

import { state, CONTRACTS, log } from '../state.js';
import { SHAPES, makeGenericShape } from '../simulation/upstreams.js';
import { makeAdapter, deriveMapping } from './adapters.js';
import { clone } from './utils.js';

function genericContract() {
  return {
    version: '1.0.0', state: 'ACTIVE', sunsetDay: null,
    fields: [
      { name: 'id', type: 'string' },
      { name: 'status', type: 'enum', values: ['active', 'inactive', 'pending'] },
      { name: 'ref_id', type: 'string' },
      { name: 'updated_at', type: 'datetime' }
    ]
  };
}

const ID_RE = /^[a-z][a-z0-9_.]{1,40}$/i;

export function addBinding({ id, displayName, kind, system, iface }) {
  id = (id || '').trim();
  if (!id) return { ok: false, reason: 'Give the API a name.' };
  if (!ID_RE.test(id)) return { ok: false, reason: 'Use letters, numbers, dots, or underscores, starting with a letter.' };
  if (state.bindings.some(b => b.id === id)) return { ok: false, reason: `An API called "${id}" already exists.` };
  if (kind !== 'REST' && kind !== 'FILE') kind = 'REST';

  CONTRACTS[id] = genericContract();
  const shape = makeGenericShape(kind);
  SHAPES[id] = { v1: shape };

  const b = {
    id, tool: id, kind, system: system || 'custom', iface: iface || (kind === 'FILE' ? `${id}.csv` : `GET /${id}`),
    displayName: displayName || id,
    prefix: (id.replace(/[^a-z0-9]/gi, '').slice(0, 3) || 'API').toUpperCase() + '-',
    altStatus: 'state', newState: { truth: 'archived', up: 'ARCHIVED' }, ver: 'v1',
    canon: CONTRACTS[id].fields.map(f => f.name),
    newActive: false, silentActive: false, proposalFailed: null, adapters: [],
    upstream: { versions: { v1: clone(shape) }, sunsetDay: null, sunsetVersion: null },
    pristine: { v1: clone(shape) }
  };
  b.adapters.push(makeAdapter(b, 'v1', deriveMapping(b, shape), 'primary', 'initial'));
  state.bindings.push(b);
  if (!state.sel) state.sel = id;
  log(b, 'SYSTEM', `API "${b.displayName}" (${id}, ${kind}) added`, 'sim', { actor: 'operator', action: 'API_ADDED' });
  return { ok: true, binding: b };
}

// Refuses to remove the last remaining API — most of the UI assumes there
// is always a selected binding to show, and "zero APIs configured" isn't a
// state worth building every view around for a prototype at this stage.
export function removeBinding(id) {
  if (state.bindings.length <= 1) return { ok: false, reason: 'At least one API has to stay configured.' };
  const idx = state.bindings.findIndex(b => b.id === id);
  if (idx === -1) return { ok: false, reason: 'That API no longer exists.' };
  const b = state.bindings[idx];
  state.bindings.splice(idx, 1);
  state.reviews = state.reviews.filter(r => r.bindingId !== id);
  state.scenarioQueue = state.scenarioQueue.filter(q => q.bindingId !== id);
  delete CONTRACTS[id];
  delete SHAPES[id];
  if (state.sel === id) state.sel = state.bindings[0].id;
  if (state.selAdapter && state.selAdapter.startsWith(id + '/')) state.selAdapter = null;
  log(null, 'SYSTEM', `API "${b.displayName || id}" (${id}) removed`, 'sim', { actor: 'operator', action: 'API_REMOVED' });
  return { ok: true };
}
