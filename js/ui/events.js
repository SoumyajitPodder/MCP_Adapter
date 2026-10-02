// Wires DOM events (button clicks, the review dropdowns, the speed
// control, the audit search) to the controller and the simulation, then
// re-renders. This is the only file that listens on the document;
// render.js only ever produces markup.
//
// Controller operations are async (a real upstream takes real time), so
// handlers await them and re-render when they finish. Work that doesn't
// touch the controller still happens before the first await, so the UI
// responds to those clicks immediately.

import { $ } from './dom.js';
import { controller, sim, clock, ui } from '../ctx.js';
import { slugify } from '../../controller/utils.js';
import { render, animate, renderClock, renderAuditTab } from './render.js';

let autoTimer = null;
let batchRunning = false;

const narrow = () => window.innerWidth <= 900;

// The clock ticks every 100ms of real time; how many simulated minutes
// that represents depends on the selected speed. At 1x, a full simulated
// day takes BASE_DAY_MS.
const TICK_MS = 100;
const BASE_DAY_MS = 6000;
const minutesPerTick = () => 1440 / ((BASE_DAY_MS / ui.speed) / TICK_MS);

// One scheduled run: land whatever upstream changes are due (optionally
// after moving to the next day), then have the controller check everything.
async function runDay(advance) {
  sim.beforeBatch(advance);
  await controller.runBatch();
  ui.selStage = null;
}

async function tick() {
  if (batchRunning) return; // a slow upstream must not stack batches
  clock.state.clockMinutes += minutesPerTick();
  if (clock.state.clockMinutes >= 1440) {
    batchRunning = true;
    try { await runDay(true); } finally { batchRunning = false; }
    animate();
  } else {
    renderClock();
  }
}

function updateAutoBtn() {
  $('#autoBtn').textContent = 'Auto: ' + (autoTimer ? `on (${ui.speed}×)` : 'off');
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
// is selected in the sidebar, so actions that target an adapter look it up
// by id across every binding.
const findByAdapterId = id => controller.state.bindings.find(bb => bb.adapters.some(x => x.id === id));

// onReset is passed in by app.js rather than imported, so this file never
// needs to import the composition root.
export function bindEvents({ onReset }) {
  document.addEventListener('click', async e => {
    const t = e.target.closest('[data-act]');
    if (!t || t.tagName === 'SELECT') return;
    const act = t.dataset.act, arg = t.dataset.arg;
    const b = controller.state.bindings.find(x => x.id === ui.sel);

    try {
      switch (act) {
        case 'run': await runDay(false); animate(); break;
        case 'next': await runDay(true); animate(); break;
        case 'auto': autoTimer ? stopAuto() : startAuto(); break;
        case 'reset': stopAuto(); await onReset(); render(); break;
        case 'sel': ui.sel = arg; ui.selAdapter = null; ui.selStage = null; ui.reveal = 6; render(); break;
        case 'seladapter': ui.selAdapter = arg; ui.selStage = null; render(); break;
        case 'stage': ui.selStage = +arg; render(); break;
        case 'inject': sim.queueDrift(b, arg); render(); break;
        case 'call': ui.calls[b.id] = await controller.liveCall(b.id); render(); break;
        case 'startmigration': {
          const bb = controller.binding(t.dataset.binding);
          // A card that asked a person to confirm fields passes their choices along.
          if (bb) await controller.beginMigration(bb.id, arg, { choices: t.dataset.rv ? ui.migrationChoices[t.dataset.rv] : undefined });
          ui.selStage = null; animate(); break;
        }
        case 'promote': { const bb = findByAdapterId(arg); if (bb) await controller.promote(bb.id, arg); render(); break; }
        case 'retire': { const bb = findByAdapterId(arg); if (bb) await controller.retire(bb.id, arg); render(); break; }
        case 'breakcanary': { const bb = findByAdapterId(arg); if (bb) sim.injectCanaryFault(bb, arg); render(); break; }
        case 'deprecate': await controller.deprecateContract(arg); render(); break;
        case 'approve': await controller.approve(arg); ui.selStage = null; animate(); break;
        case 'reject': await controller.reject(arg); render(); break;
        case 'viewbatch': ui.selBatch = +arg; render(); break;
        case 'scenario': sim.activateScenario(arg); render(); break;
        case 'cancelqueue': sim.cancelQueuedDrift(arg); render(); break;
        case 'togglesidebar':
          ui.sidebarOpen = !ui.sidebarOpen;
          if (ui.sidebarOpen && narrow()) ui.rightOpen = false; // on a phone the two panels would cover each other
          render();
          break;
        case 'toggleright':
          ui.rightOpen = !ui.rightOpen;
          if (ui.rightOpen && narrow()) ui.sidebarOpen = false;
          render();
          break;
        case 'maintab': ui.mainTab = arg; render(); break;
        case 'addapi': {
          const nameInput = $('#apiNameInput'), kindInput = $('#apiKindInput'), msg = $('#addApiMsg');
          const name = nameInput.value.trim();
          // Checked on the raw name: slugify('') falls back to "api", which
          // would silently swallow a genuinely blank submission.
          const result = name ? await sim.addGenericBinding({ id: slugify(name), displayName: name, kind: kindInput.value }) : { ok: false, reason: 'Give the API a name.' };
          if (msg) { msg.textContent = result.ok ? `Added "${result.binding.displayName}".` : result.reason; msg.classList.toggle('errtext', !result.ok); }
          if (result.ok) { nameInput.value = ''; ui.sel = result.binding.id; }
          render();
          break;
        }
        case 'removeapi': {
          if (ui.confirmRemove === arg) {
            ui.confirmRemove = null;
            // Most views assume there is always a selected API to show.
            const result = controller.state.bindings.length <= 1 ? { ok: false, reason: 'At least one API has to stay configured.' } : await controller.removeBinding(arg);
            const msg = $('#addApiMsg');
            if (!result.ok && msg) { msg.textContent = result.reason; msg.classList.add('errtext'); }
          } else {
            ui.confirmRemove = arg;
          }
          render();
          break;
        }
      }
    } catch (err) { console.error(`[ui] "${act}" failed:`, err); }
  });

  document.addEventListener('change', async e => {
    const t = e.target;
    try {
      if (t.dataset && t.dataset.act === 'choose') {
        await controller.setReviewChoice(t.dataset.rv, +t.dataset.i, t.value || null);
        render();
      }
      if (t.dataset && (t.dataset.act === 'choosesrc' || t.dataset.act === 'choosevalue')) {
        // A person is confirming the parts of a migration the controller wouldn't guess.
        const picks = ui.migrationChoices[t.dataset.rv] = ui.migrationChoices[t.dataset.rv] || { fields: {}, values: {} };
        const [bucket, key] = t.dataset.act === 'choosesrc' ? [picks.fields, t.dataset.target] : [picks.values, t.dataset.key];
        if (t.value) bucket[key] = t.value; else delete bucket[key];
        const rv = controller.state.reviews.find(r => r.id === t.dataset.rv);
        // Re-run the proposal with those picks so the card shows what is still open.
        ui.migrationPreview[rv.id] = await controller.previewMigration(rv.bindingId, rv.targetVersion, picks);
        render();
      }
      if (t.id === 'speedSel') { ui.speed = +t.value; if (autoTimer) startAuto(); renderClock(); }
      if (t.id === 'auditActor') { ui.auditQuery.actor = t.value; renderAuditTab(); }
      if (t.id === 'auditGroup') { ui.auditQuery.group = t.value; renderAuditTab(); }
    } catch (err) { console.error('[ui] change handler failed:', err); }
  });

  // Live-filters as you type. Only touches the audit table's rows, never
  // the search box itself, so typing doesn't lose focus mid-search.
  document.addEventListener('input', e => {
    if (e.target.id === 'auditSearch') { ui.auditQuery.text = e.target.value; renderAuditTab(); }
  });
}
