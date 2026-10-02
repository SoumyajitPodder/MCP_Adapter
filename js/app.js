// Composition root. Builds the three starting bindings from configuration
// data and the simulated upstreams, wires rendering and DOM events, and
// exposes a console API (window.__core) for demoing without clicking.
//
// The controller itself (../controller) knows nothing of any of this.

import { controller, sim, clock, ui, resetUi } from './ctx.js';
import { BINDING_DEFS } from '../data/bindings.js';
import { makeContracts } from '../data/contracts.js';
import { primary } from '../controller/utils.js';
import { render } from './ui/render.js';
import { bindEvents } from './ui/events.js';

// Resets everything and builds the three Phase 1 bindings, each starting
// with a healthy primary adapter, then runs the first sanity batch. Used on
// page load and by the Reset button.
async function initApp() {
  await controller.reset(); sim.reset(); resetUi();
  const contracts = makeContracts();
  for (const d of BINDING_DEFS) {
    const r = await sim.addBinding(Object.assign({}, d, { contract: contracts[d.tool] }), { quiet: true });
    if (!r.ok) throw new Error(`could not register ${d.id}: ${r.reason}`);
  }
  sim.beforeBatch(false);
  await controller.runBatch();
  controller.audit(null, 'SYSTEM', `controller started; baselines captured for ${BINDING_DEFS.length} bindings`, 'batch', { actor: 'system', action: 'SYSTEM_START' });
}

// A read-only view that merges the controller's, the simulation's, the
// clock's and the UI's state under one object — for devtools and tests only.
// Nothing in the app reads through it.
const owners = () => [ui, controller.state, sim.state, clock.state];
const stateView = new Proxy({}, {
  get: (_, k) => { for (const o of owners()) if (k in o) return o[k]; },
  set: (_, k, v) => { for (const o of owners()) if (k in o) { o[k] = v; return true; } ui[k] = v; return true; },
  has: (_, k) => owners().some(o => k in o)
});

// Console interface, e.g.:
//   await __core.runBatch(true);   __core.controller.state.reviews
window.__core = {
  controller, sim, clock, ui,
  initState: initApp,
  getState: () => stateView,
  runBatch: async advance => { sim.beforeBatch(!!advance); await controller.runBatch(); },
  runPipeline: (b, a) => controller.runPipeline(b, a),
  liveCall: b => controller.liveCall(b), health: b => controller.health(b), primary,
  promote: (b, a) => controller.promote(b, a), retire: (b, a) => controller.retire(b, a),
  approve: rv => controller.approve(rv), reject: rv => controller.reject(rv),
  beginMigration: (b, v, o) => controller.beginMigration(b, v, o), assessMigrationReview: b => controller.assessMigrationReview(b),
  deprecateContract: b => controller.deprecateContract(b),
  inject: (b, id) => sim.inject(b, id), injectCanaryFault: (b, a) => sim.injectCanaryFault(b, a),
  activateScenario: id => sim.activateScenario(id), queueDrift: (b, id) => sim.queueDrift(b, id), cancelQueuedDrift: id => sim.cancelQueuedDrift(id),
  addBinding: a => sim.addGenericBinding(a), removeBinding: id => controller.removeBinding(id)
};

bindEvents({ onReset: initApp });
await initApp();
render();
