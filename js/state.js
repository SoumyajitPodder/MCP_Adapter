// The single mutable application state object. Every other module reads
// or writes through this file rather than keeping its own copy, so the UI,
// the simulator, and the core pipeline always agree on what "now" is.
//
// This file intentionally knows nothing about drift detection, adapters,
// or rendering — it only owns the state shape, the log, and the reset.

import { makeContracts } from '../data/contracts.js';
import { SEV } from './core/utils.js';

export let state = null;
export let CONTRACTS = null;

export function resetState() {
  CONTRACTS = makeContracts();
  state = {
    day: 0,
    seq: 0,
    batches: 0,
    log: [],
    reviews: [],
    metrics: { absorbed: 0, blocked: 0, reviews: 0, approved: 0 },
    sel: 'order.get',
    selAdapter: null,
    selStage: null,
    reveal: 6,
    calls: {},
    bindings: [],
    batchHistory: [],
    selBatch: null
  };
}

// Appends one line to the drift/lifecycle feed shown in the UI.
export function log(b, cls, msg, src) {
  state.seq++;
  state.log.unshift({ seq: state.seq, day: state.day, binding: b ? b.id : 'system', cls, msg, src: src || 'batch' });
  if (state.log.length > 80) state.log.pop();
}

// Logs a single classified drift item once per adapter (de-duplicated by
// fingerprint) and updates the absorbed/blocked counters.
export function logDrift(b, a, it, src) {
  const fp = it.event.fp || it.event.type;
  if (a.seen[fp]) return;
  a.seen[fp] = 1;
  if (it.tier === 'A') state.metrics.absorbed++;
  else if (SEV[it.cls] >= 2) state.metrics.blocked++;
  log(b, it.cls, `${a.id.split('/')[1]}: ${it.event.type.toLowerCase().replace(/_/g, ' ')}, ${it.reason}`, src);
}
