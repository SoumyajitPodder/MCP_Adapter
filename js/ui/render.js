// Renders the current application state into the DOM. Nothing in this file
// computes drift, classification, or lifecycle decisions — it only reads
// state and turns it into markup. Everything the person can click is wired
// up separately, in events.js.
//
// Layout: a toggleable left sidebar (scenarios, the drift queue, bindings,
// manual drift injectors), a toggleable right panel (reviews that need a
// person, and drift the controller caught and fixed on its own), and a
// tabbed workarea between them (Pipeline, Mapping, Audit Log). The stats
// bar and batch overview sit above the workarea, since they summarize
// across every binding rather than belonging to one.

import { $, esc } from './dom.js';
import { controller, sim, clock, ui } from '../ctx.js';
import { primary, adapterLabel, versionLabel } from '../../controller/utils.js';
import { SCENARIOS } from '../../data/scenarios.js';

let animTimer = null;
const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const selB = () => controller.state.bindings.find(b => b.id === ui.sel) || controller.state.bindings[0];
const selA = b => b.adapters.find(a => a.id === ui.selAdapter && a.state !== 'retired') || primary(b);

const pct = n => Math.round(n * 100);
const confColor = score => score >= 0.8 ? 'var(--pass)' : score >= 0.6 ? 'var(--review)' : 'var(--fail)';

// Renders the classifier's 0.5 name + 0.2 type + 0.3 value-shape breakdown
// as a bar chart, so a probable-rename review shows its reasoning instead
// of just a bare "confidence 0.72" number.
function confBlock(rn) {
  return `<div class="confblock">
    <div class="conflabel">Mapping confidence — "${esc(rn.from)}" → "${esc(rn.to)}"</div>
    <div class="confbar"><div class="conffill" style="width:${pct(rn.score)}%;background:${confColor(rn.score)}"></div><span class="confpct">${pct(rn.score)}%</span></div>
    <div class="confrow"><span>Name similarity</span><div class="confbar sm"><div class="conffill" style="width:${pct(rn.name)}%"></div></div><b>${pct(rn.name)}%</b></div>
    <div class="confrow"><span>Type compatibility</span><div class="confbar sm"><div class="conffill" style="width:${pct(rn.type)}%"></div></div><b>${pct(rn.type)}%</b></div>
    <div class="confrow"><span>Value shape</span><div class="confbar sm"><div class="conffill" style="width:${pct(rn.value)}%"></div></div><b>${pct(rn.value)}%</b></div>
  </div>`;
}

// Reveals the sanity pipeline's six stages one at a time so a viewer can
// follow the sequence, instead of the whole pipeline appearing at once.
// Only the pipeline tab is redrawn on each tick. Redrawing the whole page
// here would rebuild every sidebar button several times a second, and a
// click that starts on a button which then gets replaced before the mouse
// comes back up is silently dropped — that made the sidebar feel dead for
// a second after every batch.
export function animate() {
  clearInterval(animTimer);
  if (reduce) { ui.reveal = 6; render(); return; }
  ui.reveal = 0; render();
  animTimer = setInterval(() => { ui.reveal++; if (ui.reveal >= 6) clearInterval(animTimer); renderPipelineTab(); }, 170);
}

function fmtClock(mins) {
  const total = Math.floor(mins);
  const h = Math.floor(total / 60) % 24;
  const m = total % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

// Updates just the day/time/speed readouts. Called on every full render(),
// and also called on its own (cheaply, no full render) by the auto-run
// ticker between day boundaries, so the clock can animate smoothly without
// re-rendering the whole page dozens of times a second.
export function renderClock() {
  $('#day').textContent = clock.state.day;
  $('#time').textContent = fmtClock(clock.state.clockMinutes);
  const sel = $('#speedSel');
  if (sel) sel.value = String(ui.speed);
}

function renderStats() {
  const h = controller.state.bindings.filter(b => controller.health(b) === 'PASS').length;
  const open = controller.state.reviews.filter(r => r.status === 'open').length;
  $('#stats').innerHTML = [
    [`${h}/${controller.state.bindings.length}`, 'bindings passing'],
    [open, 'reviews waiting'],
    [controller.state.metrics.absorbed, 'changes absorbed automatically'],
    [controller.state.metrics.blocked, 'breaking changes blocked']
  ].map(([n, l]) => `<div class="stat"><b>${n}</b><small>${l}</small></div>`).join('');
}

const READINESS_LABEL = {
  PASS: ['✓', 'READY'], REVIEW: ['⚠', 'REVIEW'], FAIL: ['✕', 'BLOCKED'],
  SUNSET: ['•', 'SUNSET'], PENDING: ['·', 'PENDING']
};

function renderBatchOverview() {
  const el = $('#batchOverview');
  if (!controller.state.batchHistory.length) { el.innerHTML = ''; return; }
  const latest = controller.state.batchHistory[0];
  const cur = controller.state.batchHistory.find(x => x.n === ui.selBatch) || latest;

  const rows = cur.results.map(r => {
    const [sym, label] = READINESS_LABEL[r.readiness] || ['·', r.readiness];
    return `<tr><td>${esc(r.tool)}</td><td><span class="pill ${r.readiness}">${sym} ${label}</span></td></tr>`;
  }).join('');

  const hist = controller.state.batchHistory.slice(0, 12).map(x => {
    const counts = x.results.reduce((m, r) => { m[r.readiness] = (m[r.readiness] || 0) + 1; return m; }, {});
    const summary = ['PASS', 'REVIEW', 'FAIL'].map(k => counts[k] ? `${counts[k]} ${READINESS_LABEL[k][1].toLowerCase()}` : null).filter(Boolean).join(', ') || 'no issues';
    return `<button class="bhistchip ${x.n === cur.n ? 'sel' : ''}" data-act="viewbatch" data-arg="${x.n}">#${x.n} · day ${x.day}<span class="tiny">${summary}</span></button>`;
  }).join('');

  el.innerHTML = `
    <div class="batchhead">
      <div class="brun">BATCH RUN #${cur.n}</div>
      <div class="bday">day ${cur.day}${cur.n !== latest.n ? ' · viewing history' : ''}</div>
    </div>
    <hr class="brule">
    <div class="bsummary">${cur.results.length} bindings evaluated<br>${cur.sourcesChecked} API/file sources checked</div>
    <table class="btable"><tbody>${rows}</tbody></table>
    <hr class="brule">
    ${controller.state.batchHistory.length > 1 ? `<div class="bhistrow">${hist}</div>` : ''}
  `;
}

/* =========================== sidebar =========================== */

function renderBindings() {
  $('#bcount').textContent = controller.state.bindings.length + ' tools';
  $('#bindings').innerHTML = controller.state.bindings.map(b => {
    const h = controller.health(b), p = primary(b);
    return `<button class="bnd ${b.id === ui.sel ? 'sel' : ''}" data-act="sel" data-arg="${b.id}">
      <div class="r1"><span class="nm">${esc(b.displayName || b.tool)}</span><span class="pill ${h}">${h}</span></div>
      <div class="r2">${b.kind} from ${b.system}<br>serving ${versionLabel(b, p)}</div></button>`;
  }).join('');
}

function renderSim() {
  const b = selB();
  const pending = id => sim.state.scenarioQueue.some(q => q.bindingId === b.id && q.injectId === id);
  const versionPending = sim.state.scenarioQueue.some(q => q.bindingId === b.id && sim.INJ.find(i => i.id === q.injectId && i.custom));
  $('#sim').innerHTML = `<p class="tiny" style="margin-bottom:8px">Applies to <b>${esc(b.displayName || b.tool)}</b> (${b.kind}). Each click adds to the drift queue above; the change lands at the next batch run, and can be cancelled until then.</p><div class="sim-grid">` +
    sim.INJ.filter(i => i.kinds.includes(b.kind)).map(i => {
      const off = pending(i.id) || (i.custom && (sim.retirementPending(b) || versionPending));
      return `<button class="btn sm ${i.custom ? 'warnish' : ''}" data-act="inject" data-arg="${i.id}" ${off ? 'disabled' : ''}>${i.label}</button>`;
    }).join('') + `</div>`;
}

function isBindingBusy(bindingId) {
  return SCENARIOS.some(s => sim.state.activeScenarios.includes(s.id) && s.bindingId === bindingId);
}

function renderScenarios() {
  $('#scenarios').innerHTML = SCENARIOS.map(sc => {
    const mine = sim.state.activeScenarios.includes(sc.id);
    const busy = isBindingBusy(sc.bindingId);
    const label = mine ? 'In the queue' : busy ? 'Binding busy' : 'Add scenario';
    const why = !mine && busy ? ' title="Another scenario is already queued for this binding"' : '';
    return `<div class="scenario">
      <div class="scenname">${esc(sc.name)}</div>
      <div class="scendesc">${esc(sc.description)}</div>
      <div class="tiny">on <b>${esc(sc.bindingId)}</b> · ${sc.steps.length} scheduled event${sc.steps.length === 1 ? '' : 's'}</div>
      <button class="btn sm ${busy ? '' : 'primary'}" data-act="scenario" data-arg="${sc.id}" ${busy ? 'disabled' : ''}${why}>${label}</button>
    </div>`;
  }).join('');
}

// Everything that is about to happen to an upstream — manual injections
// and scenario steps alike — with a way to call any of it off.
function renderDriftQueue() {
  const q = [...sim.state.scenarioQueue].sort((a, b) => a.day - b.day);
  $('#qcount').textContent = q.length ? `${q.length} pending` : '';
  $('#driftQueue').innerHTML = q.length
    ? `<ul class="upqueue">${q.map(u => `<li>
        <div><b>${u.day <= clock.state.day ? 'next batch' : 'day ' + u.day}</b> · ${esc(u.bindingId)}</div>
        <div class="tiny">${esc(u.note)} <span class="qsrc">${esc(u.scenarioName)}</span></div>
        <button class="btn sm cancelq" data-act="cancelqueue" data-arg="${u.id}">Cancel</button>
      </li>`).join('')}</ul>`
    : '<p class="empty">Nothing queued. Add a scenario above, or queue a manual injection below.</p>';
}

/* ============================ pipeline tab ============================ */

function stageClass(i, run) {
  if (!run) return 'pending';
  if (i >= ui.reveal) return 'pending';
  return run.stages[i].status;
}

function renderPipelineTab() {
  const b = selB(), a = selA(b), c = b.contract, run = a.lastRun;
  const names = ['Version check', 'Schema comparison', 'Adapter compatibility', 'Smoke test', 'Canonical validation', 'Readiness'];
  const symbol = { pass: '✓', warn: '!', fail: '×', skip: '–', pending: '·' };
  const selStage = ui.selStage != null ? ui.selStage : (run ? Math.max(0, run.stages.findIndex(s => s.status === 'fail' || s.status === 'warn')) : 0);
  const tabs = b.adapters.filter(x => x.state !== 'retired').map(x =>
    `<button class="tab ${x.id === a.id ? 'sel' : ''}" data-act="seladapter" data-arg="${x.id}">${adapterLabel(b, x)}</button>`
  ).join('');
  const openMigration = controller.state.reviews.find(r => r.kind === 'migration' && r.bindingId === b.id && r.status === 'open');
  const migratePointer = openMigration
    ? `<div class="detail migrate-pointer"><b>This API needs attention.</b> ${esc(openMigration.reasonText)} Open <b>Reviews</b> in the right panel to see what's recommended.</div>`
    : '';
  const st = run ? run.stages[selStage] : null;
  const call = ui.calls[b.id];

  // This view is read-only on purpose: every action that changes an
  // adapter's state now lives in the migration review card instead, so
  // there is exactly one place to act and one place to observe.
  const lane = (stt, label) => `<div class="lane"><h4>${label}</h4>${b.adapters.filter(x => x.state === stt).map(x => {
    let btn = '', gate = '';
    if (stt === 'tested') gate = (x.lastRun && x.lastRun.readiness === 'PASS') ? 'passed its checks — see Reviews to promote it' : 'needs a passing batch run';
    if (stt === 'canary') {
      const ok = x.lastRun && x.lastRun.readiness === 'PASS' && x.canaryPass >= 1;
      const faultBtn = `<button class="btn sm warnish" data-act="breakcanary" data-arg="${x.id}" ${sim.hasFault(x.id) ? 'disabled' : ''}>${sim.hasFault(x.id) ? 'Failure queued' : 'Simulate canary failure'}</button>`;
      btn = faultBtn;
      gate = sim.hasFault(x.id) ? 'a correctness bug is queued — run a batch to see it surface and roll back' : (ok ? `clean so far — see Reviews to promote it` : 'needs one clean batch in canary');
    }
    if (stt === 'rolled_back') { gate = 'failed in canary and was rolled back automatically; the previous primary was never touched'; }
    if (stt === 'deprecated') btn = `<button class="btn sm" data-act="retire" data-arg="${x.id}">Retire</button>`;
    return `<div class="chip ${x.id === a.id ? 'sel' : ''}"><b>${versionLabel(b, x)}</b>mapping v${x.mapping.version}${btn}${gate ? `<span class="gate">${gate}</span>` : ''}</div>`;
  }).join('')}</div>`;

  const clane = (stt, label) => `<div class="lane" style="min-height:64px"><h4>${label}</h4>${c.state === stt ? `<div class="chip"><b>${b.tool}@${c.version}</b>${stt === 'ACTIVE' ? `<button class="btn sm" data-act="deprecate" data-arg="${b.tool}">Deprecate contract</button><span class="gate">sunset lands 5 days later</span>` : ''}${stt === 'DEPRECATED' ? `<span class="gate">sunset on day ${clock.format(c.sunsetAt)}</span>` : ''}</div>` : ''}</div>`;

  const ready = run ? `<div class="ready ${run.readiness}"><b>${run.readiness}</b><span>${esc(run.stages[5].lines[0])}. Last run on day ${run.day}, classification ${run.overall.replace('_', ' ').toLowerCase()}.</span></div>` : '';

  $('#tabPipeline').innerHTML = `
   <div class="card">
     <div class="bhead"><div><h2>${esc(b.displayName || b.tool)} <small>${esc(b.tool)}</small></h2>
       <div class="chips"><span class="pill kind">${b.kind}</span><span class="pill kind">${b.system}</span><span class="pill kind">${esc(b.iface)}</span><span class="pill ${c.state}">contract ${c.version} ${c.state}</span></div></div>
       <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn sm" data-act="call">Call tool now</button></div></div>
     <div class="tabs">${tabs}</div>
     ${migratePointer}
   </div>
   <div class="card">
     <h2>Sanity pipeline <small>${run ? `${versionLabel(b, a)}, day ${run.day}` : 'not run yet'}</small></h2>
     <div class="path">${names.map((n, i) => { const sc = stageClass(i, run); const stx = run && i < ui.reveal ? run.stages[i].status : 'pending';
       return `<button class="stn ${sc} ${i === selStage ? 'sel' : ''}" data-act="stage" data-arg="${i}"><span class="dot">${symbol[sc]}</span><span class="nm">${n}</span><span class="st">${stx === 'pending' ? 'waiting' : stx}</span></button>`; }).join('')}</div>
     ${st && ui.reveal >= 6 ? `<div class="detail"><h4>${names[selStage]}</h4><ul>${st.lines.map(l => `<li>${esc(l)}</li>`).join('')}</ul></div>` : ''}
     ${ui.reveal >= 6 ? ready : ''}
   </div>
   <div class="card">
     <h2>Adapter lifecycle <small>tested → canary → primary → deprecated → retired</small></h2>
     <div class="rail">${lane('tested', 'Tested')}${lane('canary', 'Canary')}${lane('rolled_back', 'Rolled back')}${lane('primary', 'Primary')}${lane('deprecated', 'Deprecated')}${lane('retired', 'Retired')}</div>
     <div class="sect rail-gap">Contract lifecycle</div>
     <div class="rail three">${clane('ACTIVE', 'Active')}${clane('DEPRECATED', 'Deprecated')}${clane('SUNSET', 'Sunset')}</div>
   </div>
   <div class="card">
     <h2>Live call <small>runtime path, same detector, fails closed</small></h2>
     ${call ? `<pre>${esc(JSON.stringify(call, null, 2))}</pre>` : `<p class="empty">Press "Call tool now" to see what an agent would receive right now.</p>`}
   </div>`;
}

/* ============================= mapping tab ============================= */

function renderMappingTab() {
  const b = selB(), a = selA(b);
  const mp = a.mapping;
  const yaml = [`mapping ${b.tool.replace('.', '-')}-${a.upstreamVersion}   version ${mp.version}`, `unwrap: ${mp.unwrap || 'none'}    source_tz: ${mp.sourceTz}    coerce_numbers: ${mp.coerce}`, 'fields:']
    .concat(mp.fields.map(f => `  ${f.src.padEnd(16)} → ${f.target.padEnd(12)} ${f.transform}${f.transform === 'enum' ? ' ' + JSON.stringify(f.values) : ''}${f.transform === 'datetime' ? ' ' + f.format : ''}`)).join('\n');
  const tabs = b.adapters.filter(x => x.state !== 'retired').map(x =>
    `<button class="tab ${x.id === a.id ? 'sel' : ''}" data-act="seladapter" data-arg="${x.id}">${adapterLabel(b, x)}</button>`
  ).join('');
  $('#tabMapping').innerHTML = `
    <div class="card">
      <h2>${esc(b.displayName || b.tool)} <small>${esc(b.tool)} — registered mapping for the selected adapter</small></h2>
      <div class="tabs">${tabs}</div>
    </div>
    <div class="card"><h2>Active mapping <small>${versionLabel(b, a)}</small></h2><pre>${esc(yaml)}</pre></div>`;
}

/* ======================= right panel: reviews + auto-fixed ======================= */

// A compact confidence bar per field, rather than the full name/type/value
// breakdown — a migration can touch several fields at once, and the point
// here is "which of these can I trust", not the reasoning behind each one
// (that's still one click away, in the Mapping tab, once it's running).
function migrationFieldChart(fieldScores) {
  return `<div class="confblock"><div class="conflabel">Field mapping confidence</div>` +
    fieldScores.map(fs => `<div class="confrow"><span>${esc(fs.target)}</span><div class="confbar sm"><div class="conffill" style="width:${pct(fs.score)}%;background:${confColor(fs.score)}"></div></div><b>${fs.to ? pct(fs.score) + '%' : 'no match'}</b></div>`).join('') +
    `</div>`;
}

// One card per binding that needs a migration, with exactly one
// recommended next step at a time — start it, promote it, or try again —
// so approving a migration never requires knowing what "canary" means
// going in. The confidence chart is the same one a probable rename shows;
// it's what the recommendation is based on, not a separate claim.
function migrationCard(r) {
  const b = controller.state.bindings.find(x => x.id === r.bindingId);
  const active = r.adapterId ? b.adapters.find(x => x.id === r.adapterId) : null;
  let body = '', action = '', tone = 'REVIEW';

  if (r.stage === 'not_started') {
    const prev = r.preview;
    if (!prev) {
      body = `<p>The next version couldn't be reached to check yet. This will update on the next batch.</p>`; tone = 'FAIL';
    } else if (prev.ok) {
      body = `<p>This can be generated automatically, with high confidence on every field. <b>Recommended: start the migration.</b></p>${migrationFieldChart(prev.fieldScores)}`;
      action = `<button class="btn primary sm" data-act="startmigration" data-arg="${esc(r.targetVersion)}" data-binding="${b.id}">Start migration to ${esc(r.targetVersion)}</button>`;
      tone = 'PASS';
    } else {
      if (r.canFallback === false) {
        // This upstream can't supply a mapping on its own, so a person confirms
        // which upstream field holds each unresolved one and what each
        // unfamiliar status code means. The preview is recomputed on every pick.
        const picks = ui.migrationChoices[r.id] || { fields: {}, values: {} };
        const live = ui.migrationPreview[r.id] || prev;
        const needFields = prev.fieldScores.filter(fs => !fs.resolved);
        const fieldSel = needFields.map(fs => `<div class="choicerow"><span>${esc(fs.target)}: which upstream field holds this?</span><select data-act="choosesrc" data-rv="${r.id}" data-target="${esc(fs.target)}"><option value="">choose…</option>${(fs.candidates || []).map(c => `<option value="${esc(c.path)}" ${picks.fields[fs.target] === c.path ? 'selected' : ''}>${esc(c.path)} (${pct(c.score)}% match)</option>`).join('')}</select></div>`).join('');
        const valueSel = (live.unresolvedValues || []).map(v => `<div class="choicerow"><span>${esc(v.target)}: the upstream value "${esc(v.value)}" means…</span><select data-act="choosevalue" data-rv="${r.id}" data-key="${esc(v.key)}"><option value="">choose…</option>${v.options.map(o => `<option value="${esc(o)}" ${picks.values[v.key] === o ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select></div>`).join('');
        const ready = needFields.every(fs => picks.fields[fs.target]) && live.ok;
        body = `<p>Some fields couldn't be matched with confidence, and this upstream can't supply a mapping on its own. <b>A person needs to confirm</b> which upstream field holds each one${(live.unresolvedValues || []).length ? ', and what its unfamiliar values mean' : ''} — the closest candidates are ranked below.</p>${migrationFieldChart(live.fieldScores)}${fieldSel}${valueSel}`;
        action = `<button class="btn primary sm" data-act="startmigration" data-arg="${esc(r.targetVersion)}" data-binding="${b.id}" data-rv="${r.id}" ${ready ? '' : 'disabled'}>Start migration with these choices</button>`;
      } else {
        body = `<p>Some fields couldn't be matched automatically with confidence (shown below). <b>Recommended: start the migration anyway</b> — low-confidence fields fall back to a safe, exact-name mapping, and can be corrected afterward from the Mapping tab.</p>${migrationFieldChart(prev.fieldScores)}`;
        action = `<button class="btn primary sm" data-act="startmigration" data-arg="${esc(r.targetVersion)}" data-binding="${b.id}">Start migration to ${esc(r.targetVersion)} (safe fallback)</button>`;
      }
    }
  } else if (r.stage === 'checking') {
    body = `<p>Checking the candidate — this will update on the next batch.</p>`;
  } else if (r.stage === 'ready_for_canary') {
    const usedFallback = active && active.note === 'operator authored';
    body = `<p>The candidate passed its initial checks${usedFallback ? ' using the safe fallback mapping' : ''}. <b>Recommended: promote it to canary</b> to test it safely alongside the current version before it takes over.</p>`;
    action = `<button class="btn primary sm" data-act="promote" data-arg="${active.id}">Promote to canary</button>`;
    tone = 'PASS';
  } else if (r.stage === 'in_canary') {
    body = `<p>The candidate is running in canary. <b>Recommended: run another batch</b> to complete a clean canary cycle before promoting it further.</p>`;
  } else if (r.stage === 'ready_for_primary') {
    body = `<p>The candidate has run cleanly in canary. <b>Recommended: promote it</b> to take over as the current version.</p>`;
    action = `<button class="btn primary sm" data-act="promote" data-arg="${active.id}">Promote to current version</button>`;
    tone = 'PASS';
  } else if (r.stage === 'rolled_back') {
    body = `<p>The candidate failed testing and was rolled back automatically. <b>The original version was never affected and is still running.</b> Recommended: try again.</p>`;
    action = `<button class="btn primary sm" data-act="startmigration" data-arg="${esc(r.targetVersion)}" data-binding="${b.id}">Try migration again</button>`;
    tone = 'FAIL';
  } else if (r.stage === 'no_target') {
    body = `<p>There is no newer version available to migrate to. <b>This can't be fixed from here</b> — it needs attention outside this tool, such as contacting whoever maintains this API.</p>`;
    tone = 'FAIL';
  }

  return `<div class="rev migration"><header><b>${r.id} ${esc(b.displayName || b.tool)}</b><span class="pill ${tone}">${(r.stage || '').replace(/_/g, ' ')}</span></header>
    <div class="tiny">opened on day ${r.day}${r.attempts ? ` · attempt ${r.attempts + 1}` : ''}</div>
    <p class="migreason">${esc(r.reasonText)}</p>
    ${body}
    ${action ? `<div class="row">${action}</div>` : ''}
  </div>`;
}

function reviewCard(r) {
  const b = controller.state.bindings.find(x => x.id === r.bindingId), a = b.adapters.find(x => x.id === r.adapterId);
  const live = r.eval; // kept current by the controller (on every batch and on every choice)
  const sb = live.sandbox, sh = live.shadow, allChosen = r.choices.every(c => c.selected);
  const gateOk = r.status === 'open' && allChosen && sb.valid === sb.total && sh.diffs.length === 0;
  return `<div class="rev"><header><b>${r.id} ${r.bindingId}</b><span class="pill ${r.status}">${r.status}</span></header>
    <div class="tiny">opened on day ${r.day} by ${r.src}</div>
    <ul>${r.events.map(e => `<li>${esc(e)}</li>`).join('')}</ul>
    <div class="tiny"><b>Candidate mapping changes</b></div>
    <ul>${r.changes.map(c => `<li>${esc(c)}</li>`).join('')}</ul>
    ${(r.renames && r.renames.length) ? r.renames.map(rn => confBlock(rn)).join('') : ''}
    ${r.choices.map((c, i) => `<div class="kv"><span>${esc(c.target)}: "${esc(c.value)}" means</span><select data-act="choose" data-rv="${r.id}" data-i="${i}"><option value="">choose…</option>${c.options.map(o => `<option ${c.selected === o ? 'selected' : ''}>${o}</option>`).join('')}</select></div>`).join('')}
    <div class="kv"><span>Sandbox replay</span><b>${sb.valid}/${sb.total} valid</b></div>
    <div class="kv"><span>Shadow vs last-known-good</span><b>${sh.identical}/${sh.compared} identical${sh.skipped ? `, ${sh.skipped} new` : ''}</b></div>
    <div class="row"><button class="btn primary sm" data-act="approve" data-arg="${r.id}" ${gateOk ? '' : 'disabled'}>Approve</button><button class="btn sm" data-act="reject" data-arg="${r.id}">Reject</button></div>
    <span class="gate">${gateOk ? 'gate open: sandbox all valid, no shadow differences' : 'approval needs every choice made, a fully valid sandbox, and zero shadow differences'}</span>
  </div>`;
}

// Open reviews are shown in full, since they need a decision. Resolved
// ones shrink to a single line so they don't crowd out the ones that matter.
function renderReviewPanel() {
  const open = controller.state.reviews.filter(r => r.status === 'open');
  const openMigrations = open.filter(r => r.kind === 'migration');
  const openMappings = open.filter(r => r.kind !== 'migration');
  const done = controller.state.reviews.filter(r => r.status !== 'open').slice(0, 5);
  $('#rcount').textContent = open.length ? `${open.length} need${open.length === 1 ? 's' : ''} a decision` : '';
  $('#reviewCard').classList.toggle('urgent', open.length > 0);
  if (!controller.state.reviews.length) {
    $('#reviewPanelBody').innerHTML = '<p class="empty">Nothing needs a decision. When the controller meets a change it cannot safely settle on its own, it lands here.</p>';
    return;
  }
  $('#reviewPanelBody').innerHTML =
    (open.length ? openMigrations.map(migrationCard).join('') + openMappings.map(reviewCard).join('') : '<p class="empty">Nothing waiting on you.</p>') +
    (done.length ? `<div class="sect">Resolved</div>` + done.map(r =>
      `<div class="revdone"><span class="pill ${r.status}">${r.status}</span> <b>${r.id}</b> ${esc(r.bindingId)} <span class="tiny">day ${r.day}</span></div>`).join('') : '');
}

// The other half of the story: drift the controller noticed and settled
// by itself. Renames show the same name/type/value-shape confidence
// breakdown a reviewer would see; everything else is a fixed rule with no
// guesswork involved, and says so.
function renderAutoFixPanel() {
  const fixed = controller.state.log.filter(l => l.action === 'DRIFT_ABSORBED').slice(0, 8);
  $('#autoFixBody').innerHTML = fixed.length
    ? fixed.map(l => `<div class="autofix">
        <div class="tiny"><b>day ${l.day}</b> · ${esc(l.binding)}</div>
        <div>${esc(l.msg)}</div>
        ${l.confidence ? confBlock(l.confidence) : '<div class="tiny rulenote">Fixed rule, no inference involved — nothing to be unsure about.</div>'}
      </div>`).join('')
    : '<p class="empty">No drift absorbed yet. Queue a rename or an envelope change and run a batch to watch the controller handle it on its own.</p>';
}

/* ============================= audit log tab ============================= */

const ACTION_GROUP = {
  DRIFT_ABSORBED: 'Drift', DRIFT_BLOCKED: 'Drift', DRIFT_REVIEW: 'Drift',
  REVIEW_OPENED: 'Review', REVIEW_APPROVED: 'Review', REVIEW_REJECTED: 'Review',
  ADAPTER_PROMOTED: 'Adapter lifecycle', ADAPTER_DEPRECATED: 'Adapter lifecycle', ADAPTER_RETIRED: 'Adapter lifecycle',
  ADAPTER_STOOD_UP: 'Adapter lifecycle', ADAPTER_STANDUP_FAILED: 'Adapter lifecycle', ADAPTER_ROLLED_BACK: 'Adapter lifecycle',
  CONTRACT_DEPRECATED: 'Contract lifecycle', CONTRACT_SUNSET: 'Contract lifecycle',
  SIMULATION: 'Simulation', SYSTEM_START: 'System'
};
const GROUP_ORDER = ['Drift', 'Review', 'Adapter lifecycle', 'Contract lifecycle', 'Simulation', 'System', 'Other'];
const ACTOR_LABEL = { system: 'System', operator: 'Operator', reviewer: 'Reviewer', simulator: 'Simulator' };

function actionLabel(action) {
  return String(action).replace(/_/g, ' ').toLowerCase().replace(/^\w/, c => c.toUpperCase());
}

function matchesAudit(l, q) {
  if (q.actor !== 'all' && l.actor !== q.actor) return false;
  const grp = ACTION_GROUP[l.action] || 'Other';
  if (q.group !== 'all' && grp !== q.group) return false;
  if (q.text) {
    const hay = `${l.binding} ${l.actor} ${l.action} ${l.msg}`.toLowerCase();
    if (!hay.includes(q.text.toLowerCase())) return false;
  }
  return true;
}

// Renders only the audit table's rows and count — never the search box or
// the filter selects, which live as static elements in index.html. That's
// deliberate: those are text/select inputs the person is actively using,
// and replacing them via innerHTML on every keystroke would drop focus and
// cursor position mid-search.
export function renderAuditTab() {
  const q = ui.auditQuery;
  const rows = controller.state.log.filter(l => matchesAudit(l, q));
  $('#auditCount').textContent = `${rows.length} of ${controller.state.log.length} entries`;
  $('#auditRows').innerHTML = rows.length
    ? rows.map(l => `<tr>
        <td class="mono">day ${l.day}</td>
        <td><span class="pill actor-${l.actor}">${ACTOR_LABEL[l.actor] || l.actor}</span></td>
        <td>${esc(actionLabel(l.action))}</td>
        <td>${esc(l.binding)}</td>
        <td>${esc(l.msg)}</td>
      </tr>`).join('')
    : `<tr><td colspan="5" class="empty">No entries match this search.</td></tr>`;
}

function renderAuditFilterOptions() {
  const gsel = $('#auditGroup');
  if (gsel && gsel.options.length <= 1) {
    gsel.innerHTML = '<option value="all">All actions</option>' + GROUP_ORDER.map(g => `<option value="${g}">${g}</option>`).join('');
  }
}

/* ================================ APIs tab ================================ */

// One line per API: is it working, and if not, why — in the same plain
// language the review cards use, so the story is consistent whether
// someone reads it here or gets pulled into a review about it.
function apiStatusReason(b) {
  const h = controller.health(b);
  if (h === 'SUNSET') return 'Retired — no longer served.';
  const openMig = controller.state.reviews.find(r => r.kind === 'migration' && r.bindingId === b.id && r.status === 'open');
  if (openMig) return openMig.stage === 'no_target' ? `Blocked, unfixable from here — ${openMig.reasonText}` : openMig.reasonText;
  const openMap = controller.state.reviews.find(r => r.kind !== 'migration' && r.bindingId === b.id && r.status === 'open');
  if (openMap) return `Waiting on a decision — ${openMap.events[0] || 'a change needs review'}`;
  if (h === 'FAIL') return 'Blocked for a reason not yet captured as a review — check the Pipeline tab.';
  if (h === 'REVIEW') return 'Something changed and needs a decision — see Reviews.';
  return 'Working normally.';
}

function apiRow(b) {
  const p = primary(b);
  const h = controller.health(b);
  const rolledBack = b.adapters.filter(x => x.state === 'rolled_back').length;
  const confirming = ui.confirmRemove === b.id;
  return `<tr>
    <td><b>${esc(b.displayName || b.tool)}</b><div class="tiny mono">${esc(b.id)}</div></td>
    <td>${b.kind}</td>
    <td class="mono">${p ? esc(p.upstreamVersion) : '—'}</td>
    <td><span class="pill ${h}">${h}</span>${rolledBack ? `<div class="tiny">${rolledBack} rolled-back attempt${rolledBack > 1 ? 's' : ''}</div>` : ''}</td>
    <td class="tiny">${esc(apiStatusReason(b))}</td>
    <td><button class="btn sm ${confirming ? 'cancelq' : ''}" data-act="removeapi" data-arg="${b.id}">${confirming ? 'Confirm?' : 'Remove'}</button></td>
  </tr>`;
}

export function renderApisTab() {
  $('#apiCount').textContent = controller.state.bindings.length + ' configured';
  $('#apiRows').innerHTML = controller.state.bindings.map(apiRow).join('');
}

/* ================================ tabs ================================ */

const MAIN_TABS = ['pipeline', 'apis', 'mapping', 'audit'];

function renderTabBar() {
  MAIN_TABS.forEach(t => {
    const btn = $(`#mtab-${t}`);
    if (btn) btn.classList.toggle('sel', ui.mainTab === t);
  });
  MAIN_TABS.forEach(t => {
    const panel = $(`#tab${t.charAt(0).toUpperCase()}${t.slice(1)}`);
    if (panel) panel.hidden = ui.mainTab !== t;
  });
}

function renderSidebarToggle() {
  document.body.classList.toggle('sidebar-collapsed', !ui.sidebarOpen);
  const arrow = $('#pullArrow');
  if (arrow) arrow.textContent = ui.sidebarOpen ? '‹' : '›';
  const pull = $('#sidebarPull');
  if (pull) pull.title = ui.sidebarOpen ? 'Hide sidebar' : 'Show sidebar';
}

// The right panel is where a person is needed, so its handle does more
// than the left one: it carries a count, and pulses while anything is
// waiting — which stays visible even when the panel is tucked away.
function renderRightToggle() {
  document.body.classList.toggle('rightpanel-collapsed', !ui.rightOpen);
  const open = controller.state.reviews.filter(r => r.status === 'open').length;
  const arrow = $('#rightArrow');
  if (arrow) arrow.textContent = ui.rightOpen ? '›' : '‹';
  const pull = $('#rightPull');
  if (pull) {
    pull.title = ui.rightOpen ? 'Hide reviews' : (open ? `Show reviews — ${open} waiting` : 'Show reviews');
    pull.classList.toggle('alert', open > 0);
  }
  const badge = $('#rightBadge');
  if (badge) { badge.textContent = open ? String(open) : ''; badge.hidden = !open; }
}

export function render() {
  if (!controller.state.bindings.length) return; // mid-reset: the render after the reset completes will draw everything
  renderClock();
  renderStats();
  renderBatchOverview();
  renderSidebarToggle();
  renderRightToggle();
  renderScenarios();
  renderDriftQueue();
  renderBindings();
  renderSim();
  renderTabBar();
  renderPipelineTab();
  renderApisTab();
  renderMappingTab();
  renderReviewPanel();
  renderAutoFixPanel();
  renderAuditFilterOptions();
  renderAuditTab();
}
