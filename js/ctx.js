// The demo app's wiring: one controller, one simulation, one UI state
// object. render.js and events.js import from here; nothing in controller/
// does. This is the only place the three are put together.

import { createController } from '../controller/index.js';
import { createSimClock, createSimulation } from '../simulation/simulation.js';

export const clock = createSimClock();
export const controller = createController({ clock });
export const sim = createSimulation({ controller, clock });
sim.install();

// Everything that is about *looking at* the system, not the system itself.
export const ui = {};
export function resetUi() {
  Object.assign(ui, {
    sel: 'order.get', selAdapter: null, selStage: null, reveal: 6, calls: {}, mainTab: 'pipeline',
    sidebarOpen: true, rightOpen: true, auditQuery: { text: '', actor: 'all', group: 'all' },
    confirmRemove: null, selBatch: null, speed: 1, migrationChoices: {}, migrationPreview: {}
  });
}
resetUi();

// The controller no longer reaches into UI state; it announces things and
// the UI decides what to do about them.
controller.on('review.opened', () => { ui.rightOpen = true; }); // a review needs a person: make sure the panel that shows it is open
controller.on('adapter.created', ({ adapter }) => { ui.selAdapter = adapter.id; });
controller.on('batch.completed', () => { ui.selBatch = null; }); // always land on the run that just completed
controller.on('binding.removed', ({ id }) => {
  if (ui.sel === id) ui.sel = controller.state.bindings[0] ? controller.state.bindings[0].id : null;
  if (ui.selAdapter && ui.selAdapter.startsWith(id + '/')) ui.selAdapter = null;
});
