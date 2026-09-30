// Wires DOM events (button clicks, the review dropdowns, the speed
// control, the audit search) to the core actions and the simulator, then
// re-renders. This is the only file that listens on the document;
// render.js only ever produces markup.

import { $ } from './dom.js';
import { state } from '../state.js';
import {
  runBatch, liveCall, beginMigration,
  promote, retire, deprecateContract, approve, reject, injectCanaryFault,
  activateScenario, queueDrift, cancelQueuedDrift
} from '../core/lifecycle.js';
import { addBinding, removeBinding } from '../core/registry.js';
import { slugify } from '../core/utils.js';
import { render, animate, renderClock, renderAuditTab } from './render.js';

let autoTimer = null;

const narrow = () => window.innerWidth <= 900;

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

// Cards in the right panel can act on any binding, not just whichever one
// happens to be selected in the sidebar — so actions that target a
// specific adapter look the adapter up by id across every binding, rather
// than assuming it belongs to the currently selected one.
const findByAdapterId = id => state.bindings.find(bb => bb.adapters.some(x => x.id === id));

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
      case 'inject': queueDrift(b, arg); render(); break;
      case 'call': state.calls[b.id] = liveCall(b); render(); break;
      case 'startmigration': { const bb = state.bindings.find(x => x.id === t.dataset.binding); if (bb) beginMigration(bb, arg); state.selStage = null; animate(); break; }
      case 'promote': { const bb = findByAdapterId(arg); if (bb) promote(bb, bb.adapters.find(x => x.id === arg)); render(); break; }
      case 'retire': { const bb = findByAdapterId(arg); if (bb) retire(bb, bb.adapters.find(x => x.id === arg)); render(); break; }
      case 'breakcanary': { const bb = findByAdapterId(arg); if (bb) injectCanaryFault(bb, bb.adapters.find(x => x.id === arg)); render(); break; }
      case 'deprecate': deprecateContract(arg); render(); break;
      case 'approve': approve(state.reviews.find(r => r.id === arg)); state.selStage = null; animate(); break;
      case 'reject': reject(state.reviews.find(r => r.id === arg)); render(); break;
      case 'viewbatch': state.selBatch = +arg; render(); break;
      case 'scenario': activateScenario(arg); render(); break;
      case 'cancelqueue': cancelQueuedDrift(arg); render(); break;
      case 'togglesidebar':
        state.sidebarOpen = !state.sidebarOpen;
        if (state.sidebarOpen && narrow()) state.rightOpen = false; // on a phone the two panels would cover each other
        render();
        break;
      case 'toggleright':
        state.rightOpen = !state.rightOpen;
        if (state.rightOpen && narrow()) state.sidebarOpen = false;
        render();
        break;
      case 'maintab': state.mainTab = arg; render(); break;
      case 'addapi': {
        const nameInput = $('#apiNameInput'), kindInput = $('#apiKindInput'), msg = $('#addApiMsg');
        const name = nameInput.value.trim();
        // Checked here, on the raw name, rather than left to addBinding:
        // slugify() maps an empty string to the fallback id "api" so a
        // blank name doesn't collide with itself on repeat submits, but
        // that same fallback would silently swallow a genuinely blank
        // submission instead of rejecting it.
        const result = name ? addBinding({ id: slugify(name), displayName: name, kind: kindInput.value }) : { ok: false, reason: 'Give the API a name.' };
        if (msg) {
          msg.textContent = result.ok ? `Added "${result.binding.displayName}".` : result.reason;
          msg.classList.toggle('errtext', !result.ok);
        }
        if (result.ok) { nameInput.value = ''; state.sel = result.binding.id; }
        render();
        break;
      }
      case 'removeapi': {
        if (state.confirmRemove === arg) {
          const result = removeBinding(arg);
          state.confirmRemove = null;
          const msg = $('#addApiMsg');
          if (!result.ok && msg) { msg.textContent = result.reason; msg.classList.add('errtext'); }
        } else {
          state.confirmRemove = arg;
        }
        render();
        break;
      }
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
  });
}
