// Renders the current application state into the DOM. Nothing in this file
// computes drift, classification, or lifecycle decisions — it only reads
// state and turns it into markup. Everything the person can click is wired
// up separately, in events.js.

import { $, esc } from './dom.js';
import { state, CONTRACTS } from '../state.js';
import { primary } from '../core/utils.js';
import { health, untargeted, evalCandidate, withChoices } from '../core/lifecycle.js';
import { INJ } from '../simulation/upstreams.js';

let animTimer = null;
const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const selB = () => state.bindings.find(b => b.id === state.sel);
const selA = b => b.adapters.find(a => a.id === state.selAdapter && a.state !== 'retired') || primary(b);
const short = a => a.id.split('/')[1];

// Reveals the sanity pipeline's six stages one at a time so a viewer can
// follow the sequence, instead of the whole pipeline appearing at once.
export function animate() {
  clearInterval(animTimer);
  if (reduce) { state.reveal = 6; render(); return; }
  state.reveal = 0; render();
  animTimer = setInterval(() => { state.reveal++; if (state.reveal >= 6) clearInterval(animTimer); render(); }, 170);
}

function renderStats() {
  const h = state.bindings.filter(b => health(b) === 'PASS').length;
  const open = state.reviews.filter(r => r.status === 'open').length;
  $('#day').textContent = state.day;
  $('#stats').innerHTML = [
    [`${h}/${state.bindings.length}`, 'bindings passing'],
    [open, 'reviews waiting'],
    [state.metrics.absorbed, 'changes absorbed automatically'],
    [state.metrics.blocked, 'breaking changes blocked']
  ].map(([n, l]) => `<div class="stat"><b>${n}</b><small>${l}</small></div>`).join('');
}

function renderBindings() {
  $('#bcount').textContent = state.bindings.length + ' tools';
  $('#bindings').innerHTML = state.bindings.map(b => {
    const h = health(b), p = primary(b);
    return `<button class="bnd ${b.id === state.sel ? 'sel' : ''}" data-act="sel" data-arg="${b.id}">
      <div class="r1"><span class="nm">${b.tool}</span><span class="pill ${h}">${h}</span></div>
      <div class="r2">${b.kind} from ${b.system}<br>serving ${short(p)} on upstream ${p.upstreamVersion}</div></button>`;
  }).join('');
}

function renderSim() {
  const b = selB();
  $('#sim').innerHTML = `<p class="tiny" style="margin-bottom:8px">Changes apply to <b>${b.tool}</b> (${b.kind}). Inject one, then run a batch.</p><div class="sim-grid">` +
    INJ.filter(i => i.kinds.includes(b.kind)).map(i =>
      `<button class="btn sm ${i.custom ? 'warnish' : ''}" data-act="inject" data-arg="${i.id}" ${i.id === 'sunset' && b.upstream.sunsetDay != null ? 'disabled' : ''}>${i.label}</button>`
    ).join('') + `</div>`;
}

const READINESS_LABEL = {
  PASS: ['✓', 'READY'], REVIEW: ['⚠', 'REVIEW'], FAIL: ['✕', 'BLOCKED'],
  SUNSET: ['•', 'SUNSET'], PENDING: ['·', 'PENDING']
};

function renderBatchOverview() {
  const el = $('#batchOverview');
  if (!state.batchHistory.length) { el.innerHTML = ''; return; }
  const latest = state.batchHistory[0];
  const cur = state.batchHistory.find(x => x.n === state.selBatch) || latest;

  const rows = cur.results.map(r => {
    const [sym, label] = READINESS_LABEL[r.readiness] || ['·', r.readiness];
    return `<tr><td>${esc(r.tool)}</td><td><span class="pill ${r.readiness}">${sym} ${label}</span></td></tr>`;
  }).join('');

  const hist = state.batchHistory.slice(0, 12).map(x => {
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
    ${state.batchHistory.length > 1 ? `<div class="bhistrow">${hist}</div>` : ''}
  `;
}

function stageClass(i, run) {
  if (!run) return 'pending';
  if (i >= state.reveal) return 'pending';
  return run.stages[i].status;
}

function renderMain() {
  const b = selB(), a = selA(b), c = CONTRACTS[b.tool], run = a.lastRun;
  const names = ['Version check', 'Schema comparison', 'Adapter compatibility', 'Smoke test', 'Canonical validation', 'Readiness'];
  const symbol = { pass: '✓', warn: '!', fail: '×', skip: '–', pending: '·' };
  const selStage = state.selStage != null ? state.selStage : (run ? Math.max(0, run.stages.findIndex(s => s.status === 'fail' || s.status === 'warn')) : 0);
  const tabs = b.adapters.filter(x => x.state !== 'retired').map(x =>
    `<button class="tab ${x.id === a.id ? 'sel' : ''}" data-act="seladapter" data-arg="${x.id}">${short(x)} on ${x.upstreamVersion} (${x.state})</button>`
  ).join('');
  const un = untargeted(b);
  let standBtn = '';
  if (un.length) standBtn = un.map(v => `<button class="btn primary sm" data-act="standup" data-arg="${v}">Stand up adapter for ${v}</button>`).join(' ');
  let failNote = '';
  if (b.proposalFailed) {
    failNote = `<div class="detail" style="border-color:var(--fail)"><h4>Automatic mapping for ${b.proposalFailed.ver} was refused</h4><ul>${b.proposalFailed.unresolved.map(u => `<li>${esc(u)}</li>`).join('')}</ul><p class="tiny" style="margin-top:6px">Confidence is too low to propose a mapping. A person writes it; the same tests and gates then apply.</p><div style="margin-top:8px"><button class="btn sm" data-act="opmap" data-arg="${b.proposalFailed.ver}">Apply operator-authored mapping for ${b.proposalFailed.ver}</button></div></div>`;
  }
  const st = run ? run.stages[selStage] : null;
  const call = state.calls[b.id];
  const mp = a.mapping;
  const yaml = [`mapping ${b.tool.replace('.', '-')}-${a.upstreamVersion}   version ${mp.version}`, `unwrap: ${mp.unwrap || 'none'}    source_tz: ${mp.sourceTz}    coerce_numbers: ${mp.coerce}`, 'fields:']
    .concat(mp.fields.map(f => `  ${f.src.padEnd(16)} → ${f.target.padEnd(12)} ${f.transform}${f.transform === 'enum' ? ' ' + JSON.stringify(f.values) : ''}${f.transform === 'datetime' ? ' ' + f.format : ''}`)).join('\n');

  const lane = (stt, label) => `<div class="lane"><h4>${label}</h4>${b.adapters.filter(x => x.state === stt).map(x => {
    let btn = '', gate = '';
    if (stt === 'tested') { const ok = x.lastRun && x.lastRun.readiness === 'PASS'; btn = `<button class="btn sm" data-act="promote" data-arg="${x.id}" ${ok ? '' : 'disabled'}>Promote to canary</button>`; gate = ok ? 'contract tests passed' : 'needs a passing batch run'; }
    if (stt === 'canary') {
      const ok = x.lastRun && x.lastRun.readiness === 'PASS' && x.canaryPass >= 1;
      const promoteBtn = `<button class="btn sm" data-act="promote" data-arg="${x.id}" ${ok ? '' : 'disabled'}>Promote to primary</button>`;
      const faultBtn = `<button class="btn sm warnish" data-act="breakcanary" data-arg="${x.id}" ${x.fault ? 'disabled' : ''}>${x.fault ? 'Failure queued' : 'Simulate canary failure'}</button>`;
      btn = promoteBtn + faultBtn;
      gate = x.fault ? 'a correctness bug is queued — run a batch to see it surface and roll back' : (ok ? `${x.canaryPass} clean run${x.canaryPass > 1 ? 's' : ''} in canary` : 'needs one clean batch in canary');
    }
    if (stt === 'rolled_back') { gate = 'failed in canary and was rolled back automatically; the previous primary was never touched'; }
    if (stt === 'deprecated') btn = `<button class="btn sm" data-act="retire" data-arg="${x.id}">Retire</button>`;
    return `<div class="chip ${x.id === a.id ? 'sel' : ''}"><b>${short(x)}</b>upstream ${x.upstreamVersion}, mapping v${x.mapping.version}${btn}${gate ? `<span class="gate">${gate}</span>` : ''}</div>`;
  }).join('')}</div>`;

  const clane = (stt, label) => `<div class="lane" style="min-height:64px"><h4>${label}</h4>${c.state === stt ? `<div class="chip"><b>${b.tool}@${c.version}</b>${stt === 'ACTIVE' ? `<button class="btn sm" data-act="deprecate" data-arg="${b.tool}">Deprecate contract</button><span class="gate">sunset lands 5 days later</span>` : ''}${stt === 'DEPRECATED' ? `<span class="gate">sunset on day ${c.sunsetDay}</span>` : ''}</div>` : ''}</div>`;

  const ready = run ? `<div class="ready ${run.readiness}"><b>${run.readiness}</b><span>${esc(run.stages[5].lines[0])}. Last run on day ${run.day}, classification ${run.overall.replace('_', ' ').toLowerCase()}.</span></div>` : '';

  $('#main').innerHTML = `
   <div class="card">
     <div class="bhead"><div><h2>${b.tool}</h2>
       <div class="chips"><span class="pill kind">${b.kind}</span><span class="pill kind">${b.system}</span><span class="pill kind">${esc(b.iface)}</span><span class="pill ${c.state}">contract ${c.version} ${c.state}</span></div></div>
       <div style="display:flex;gap:8px;flex-wrap:wrap">${standBtn}<button class="btn sm" data-act="call">Call tool now</button></div></div>
     <div class="tabs">${tabs}</div>
     ${failNote}
   </div>
   <div class="card">
     <h2>Sanity pipeline <small>${run ? `${short(a)}, day ${run.day}` : 'not run yet'}</small></h2>
     <div class="path">${names.map((n, i) => { const sc = stageClass(i, run); const stx = run && i < state.reveal ? run.stages[i].status : 'pending';
       return `<button class="stn ${sc} ${i === selStage ? 'sel' : ''}" data-act="stage" data-arg="${i}"><span class="dot">${symbol[sc]}</span><span class="nm">${n}</span><span class="st">${stx === 'pending' ? 'waiting' : stx}</span></button>`; }).join('')}</div>
     ${st && state.reveal >= 6 ? `<div class="detail"><h4>${names[selStage]}</h4><ul>${st.lines.map(l => `<li>${esc(l)}</li>`).join('')}</ul></div>` : ''}
     ${state.reveal >= 6 ? ready : ''}
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
   </div>
   <div class="card"><h2>Active mapping <small>${short(a)}</small></h2><pre>${esc(yaml)}</pre></div>`;
}

function renderReviews() {
  const open = state.reviews.filter(r => r.status === 'open').length;
  $('#rcount').textContent = open ? open + ' waiting' : '';
  if (!state.reviews.length) { $('#reviews').innerHTML = '<p class="empty">Nothing to review. Inject a rename, a new status value, or a date format change, then run a batch.</p>'; return; }
  $('#reviews').innerHTML = state.reviews.slice(0, 6).map(r => {
    const b = state.bindings.find(x => x.id === r.bindingId), a = b.adapters.find(x => x.id === r.adapterId);
    const live = r.status === 'open' ? evalCandidate(b, a, withChoices(r)) : r.eval;
    const sb = live.sandbox, sh = live.shadow, allChosen = r.choices.every(c => c.selected);
    const gateOk = r.status === 'open' && allChosen && sb.valid === sb.total && sh.diffs.length === 0;
    return `<div class="rev"><header><b>${r.id} ${r.bindingId}</b><span class="pill ${r.status}">${r.status}</span></header>
      <div class="tiny">opened on day ${r.day} by ${r.src}</div>
      <ul>${r.events.map(e => `<li>${esc(e)}</li>`).join('')}</ul>
      <div class="tiny"><b>Candidate mapping changes</b></div>
      <ul>${r.changes.map(c => `<li>${esc(c)}</li>`).join('')}</ul>
      ${r.choices.map((c, i) => `<div class="kv"><span>${esc(c.target)}: "${esc(c.value)}" means</span>${r.status === 'open' ? `<select data-act="choose" data-rv="${r.id}" data-i="${i}"><option value="">choose…</option>${c.options.map(o => `<option ${c.selected === o ? 'selected' : ''}>${o}</option>`).join('')}</select>` : `<b>${c.selected || 'n/a'}</b>`}</div>`).join('')}
      <div class="kv"><span>Sandbox replay</span><b>${sb.valid}/${sb.total} valid</b></div>
      <div class="kv"><span>Shadow vs last-known-good</span><b>${sh.identical}/${sh.compared} identical${sh.skipped ? `, ${sh.skipped} new` : ''}</b></div>
      ${r.status === 'open' ? `<div class="row"><button class="btn primary sm" data-act="approve" data-arg="${r.id}" ${gateOk ? '' : 'disabled'}>Approve</button><button class="btn sm" data-act="reject" data-arg="${r.id}">Reject</button></div>
      <span class="gate">${gateOk ? 'gate open: sandbox all valid, no shadow differences' : 'approval needs every choice made, a fully valid sandbox, and zero shadow differences'}</span>` : ''}
    </div>`;
  }).join('');
}

function renderFeed() {
  $('#feed').innerHTML = state.log.map(l =>
    `<li><div class="meta"><span class="pill ${l.cls}">${l.cls.replace('_REQUIRED', '').replace('COMPATIBLE', 'ABSORBED')}</span><span>day ${l.day}</span><span>${esc(l.binding)}</span><span>${l.src}</span></div>${esc(l.msg)}</li>`
  ).join('');
}

export function render() {
  renderStats();
  renderBatchOverview();
  renderBindings();
  renderSim();
  renderMain();
  renderReviews();
  renderFeed();
}
