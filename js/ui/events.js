// Wires DOM events (button clicks, the review dropdowns, the speed
<<<<<<< HEAD
// control, the audit search) to the core actions and the simulator, then
// re-renders. This is the only file that listens on the document;
// render.js only ever produces markup.
=======
// control) to the core actions and the simulator, then re-renders. This is
// the only file that listens on the document; render.js only ever
// produces markup.
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105

import { $ } from './dom.js';
import { state } from '../state.js';
import { inject } from '../simulation/upstreams.js';
import {
  runBatch, liveCall, standUp, operatorMap,
  promote, retire, deprecateContract, approve, reject, injectCanaryFault,
  activateScenario
} from '../core/lifecycle.js';
<<<<<<< HEAD
import { render, animate, renderClock, renderAuditTab } from './render.js';
=======
import { render, animate, renderClock } from './render.js';
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105

let autoTimer = null;

// The clock ticks every 100ms of real time; how many simulated minutes
// that represents depends on the selected speed. At 1x, a full simulated
// day takes BASE_DAY_MS — the same pace the fixed 6-second auto-run used
// before there was a speed control.
const TICK_MS = 100;
const BASE_DAY_MS = 6000;

function minutesPerTick() {
  const dayMs = BASE_DAY_MS / state.speed;
  return 1440 / (dayMs / TICK_MS);
}

function tick() {
  state.clockMinutes += minutesPerTick();
  if (state.clockMinutes >= 1440) {
    runBatch(true);
    state.selStage = null;
    animate();
  } else {
    renderClock();
  }
}

function updateAutoBtn() {
  $('#autoBtn').textContent = 'Auto: ' + (autoTimer ? `on (${state.speed}×)` : 'off');
  $('#autoBtn').classList.toggle('on', !!autoTimer);
}

function startAuto() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = setInterval(tick, TICK_MS);
  updateAutoBtn();
}

function stopAuto() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  updateAutoBtn();
}

// onReset is passed in by app.js rather than imported directly, so this
// file never needs to import the composition root.
export function bindEvents({ onReset }) {
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-act]');
    if (!t || t.tagName === 'SELECT') return;
    const act = t.dataset.act, arg = t.dataset.arg;
    const b = state.bindings.find(x => x.id === state.sel);

    switch (act) {
      case 'run': runBatch(false); state.selStage = null; animate(); break;
      case 'next': runBatch(true); state.selStage = null; animate(); break;
      case 'auto': autoTimer ? stopAuto() : startAuto(); break;
      case 'reset':
        stopAuto();
        onReset();
        render();
        break;
      case 'sel': state.sel = arg; state.selAdapter = null; state.selStage = null; state.reveal = 6; render(); break;
      case 'seladapter': state.selAdapter = arg; state.selStage = null; render(); break;
      case 'stage': state.selStage = +arg; render(); break;
      case 'inject': inject(b, arg); render(); break;
      case 'call': state.calls[b.id] = liveCall(b); render(); break;
      case 'standup': standUp(b, arg); state.selStage = null; animate(); break;
      case 'opmap': operatorMap(b, arg); state.selStage = null; animate(); break;
      case 'promote': promote(b, b.adapters.find(x => x.id === arg)); render(); break;
      case 'retire': retire(b, b.adapters.find(x => x.id === arg)); render(); break;
      case 'deprecate': deprecateContract(arg); render(); break;
      case 'approve': approve(state.reviews.find(r => r.id === arg)); state.selStage = null; animate(); break;
      case 'reject': reject(state.reviews.find(r => r.id === arg)); render(); break;
      case 'breakcanary': injectCanaryFault(b, b.adapters.find(x => x.id === arg)); render(); break;
      case 'viewbatch': state.selBatch = +arg; render(); break;
      case 'scenario': activateScenario(arg); render(); break;
      case 'togglesidebar': state.sidebarOpen = !state.sidebarOpen; render(); break;
      case 'maintab': state.mainTab = arg; render(); break;
    }
  });

  document.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset && t.dataset.act === 'choose') {
      const rv = state.reviews.find(r => r.id === t.dataset.rv);
      rv.choices[+t.dataset.i].selected = t.value || null;
      render();
    }
    if (t.id === 'speedSel') {
      state.speed = +t.value;
      if (autoTimer) startAuto(); // restart at the new pace
      renderClock();
    }
<<<<<<< HEAD
    if (t.id === 'auditActor') { state.auditQuery.actor = t.value; renderAuditTab(); }
    if (t.id === 'auditGroup') { state.auditQuery.group = t.value; renderAuditTab(); }
  });

  // Live-filters as you type. Only touches the audit table's rows, never
  // the search box itself, so typing doesn't lose focus mid-search.
  document.addEventListener('input', e => {
    if (e.target.id === 'auditSearch') {
      state.auditQuery.text = e.target.value;
      renderAuditTab();
    }
=======
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105
  });
}
