// Composition root. This is the only file that imports from every layer:
// it builds the initial bindings from the configuration data and the
// simulated upstreams, wires up rendering and DOM events, and exposes a
// small console API for demoing the prototype without clicking through
// the UI (window.__core).

import { state, CONTRACTS, resetState, log } from './state.js';
import { clone, primary } from './core/utils.js';
import { BINDING_DEFS } from '../data/bindings.js';
import { SHAPES, inject } from './simulation/upstreams.js';
import { makeAdapter, deriveMapping } from './core/adapters.js';
import {
  runBatch, runPipeline, promote, retire, standUp, operatorMap,
  approve, reject, liveCall, health, deprecateContract,
  withChoices, evalCandidate, injectCanaryFault, activateScenario,
  queueDrift, cancelQueuedDrift
} from './core/lifecycle.js';
import { render } from './ui/render.js';
import { bindEvents } from './ui/events.js';

// Resets state and builds the three Phase 1 bindings (order.get,
// service.get, inventory.snapshot), each starting with a healthy primary
// adapter, then runs the first sanity batch. Used both on page load and by
// the Reset button.
function initApp() {
  resetState();
  BINDING_DEFS.forEach(d => {
    const shape = SHAPES[d.tool][d.ver];
    const b = Object.assign({}, d, {
      canon: CONTRACTS[d.tool].fields.map(f => f.name),
      newActive: false,
      proposalFailed: null,
      adapters: [],
      upstream: { versions: { [d.ver]: clone(shape) }, sunsetDay: null },
      pristine: { [d.ver]: clone(shape) }
    });
    b.adapters.push(makeAdapter(b, d.ver, deriveMapping(b, shape), 'primary', 'initial'));
    state.bindings.push(b);
  });
  runBatch(false);
  log(null, 'SYSTEM', 'controller started; baselines captured for 3 bindings', 'batch', { actor: 'system', action: 'SYSTEM_START' });
}

// Console interface for demoing or debugging the prototype from devtools,
// e.g.:
//   const b = window.__core.getState().bindings[0];
//   window.__core.inject(b, 'new_enum');
//   window.__core.runBatch(true);
window.__core = {
  initState: initApp,
  getState: () => state,
  CONTRACTS: () => CONTRACTS,
  runBatch, runPipeline, inject, approve, reject, standUp, operatorMap,
  promote, retire, liveCall, health, primary, withChoices, evalCandidate,
  deprecateContract, injectCanaryFault, activateScenario,
  queueDrift, cancelQueuedDrift
};

bindEvents({ onReset: initApp });
initApp();
render();
