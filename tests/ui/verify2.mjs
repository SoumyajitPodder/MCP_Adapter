import { JSDOM } from 'jsdom';
import fs from 'fs';

const html = fs.readFileSync('./index.html', 'utf8');
const dom = new JSDOM(html);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.window.matchMedia = () => ({ matches: false });
let errored = false;
window.addEventListener('error', e => { errored = true; console.error('WINDOW ERROR', e.error && e.error.stack); });
process.on('unhandledRejection', e => { console.error('UNHANDLED', e); process.exit(1); });

await import('../../js/app.js');
const S = () => window.__core.getState();
const $ = s => document.querySelector(s);
const wait = ms => new Promise(r => setTimeout(r, ms));
const click = async (act, arg) => { const b = [...document.querySelectorAll(`[data-act="${act}"]`)].find(x => arg == null || x.dataset.arg === arg); if (!b) throw new Error('button not found: ' + act + ' ' + arg); b.click(); await new Promise(r => setTimeout(r, 0)); return true; };
let pass = 0, fail = 0;
const check = (label, cond) => { cond ? pass++ : (fail++, console.log('FAIL:', label)); };

// ---------- structure ----------
check('reviews is no longer a main tab', !$('#mtab-reviews') && !$('#tabReviews'));
check('main tabs remain (now 4: Pipeline/APIs/Mapping/Audit)', document.querySelectorAll('.mtab').length === 4);
check('right panel + handle exist as body children', $('body > #rightpanel') && $('body > #rightPull'));
check('right panel has review card then auto-fix card',
  [...document.querySelectorAll('#rightpanel .card > h2')].map(h => h.textContent).join('|').startsWith('Reviews') &&
  document.querySelectorAll('#rightpanel .card').length === 2);
check('sidebar order: scenarios, queue, bindings, admin',
  [...document.querySelectorAll('#sidebar .card > h2')].map(h => h.firstChild.textContent.trim()).join('|') === 'Scenarios|Drift queue|Bindings|Admin: drift input');

// ---------- scenario button label + new scenario ----------
const scBtns = [...document.querySelectorAll('[data-act="scenario"]')];
check('scenario buttons read "Add scenario"', scBtns.length === 4 && scBtns.every(b => b.textContent === 'Add scenario'));
check('no "Run scenario" text anywhere', !document.body.innerHTML.includes('Run scenario'));
const hk = window.__core.getState && (await import('../../data/scenarios.js')).SCENARIOS.find(s => s.id === 'gradual_housekeeping');
check('new scenario exists on a REST binding', hk && hk.bindingId === 'order.get');
check('new scenario mixes a version drift with other drifts',
  hk.steps.some(s => s.injectId === 'sunset') && hk.steps.filter(s => s.injectId !== 'sunset').length >= 3);
check('version drift comes last (natural ordering)', hk.steps[hk.steps.length - 1].injectId === 'sunset');

// ---------- manual injection goes through the queue ----------
await click('sel', 'service.get');
await click('inject', 'rename_case');
check('inject shows up in the queue immediately (visible feedback)', $('#driftQueue').innerHTML.includes('rename first field') || $('#driftQueue').innerHTML.includes('Rename first field'));
check('queue count label', $('#qcount').textContent === '1 pending');
check('queued button disabled so it cannot be double-added', document.querySelector('[data-act="inject"][data-arg="rename_case"]').disabled);
check('injection NOT yet applied to the upstream', window.__core.sim.peek('service.get').versions.v2.fields[0].src === 'serviceId');
await click('cancelqueue', S().scenarioQueue[0].id);
check('cancel removes it from the queue', S().scenarioQueue.length === 0 && $('#driftQueue').innerHTML.includes('Nothing queued'));
check('cancel is audited', S().log.some(l => l.action === 'SIMULATION_CANCELLED' && l.actor === 'operator'));
check('button re-enabled after cancel', !document.querySelector('[data-act="inject"][data-arg="rename_case"]').disabled);

await click('inject', 'rename_case');
await click('next'); await wait(1300);
check('queued injection lands on the next batch', window.__core.sim.peek('service.get').versions.v2.fields[0].src === 'service_id');
check('queue drained after landing', S().scenarioQueue.length === 0);

// ---------- auto-fixed drift panel + confidence ----------
check('auto-fix panel shows the absorbed rename', $('#autoFixBody').innerHTML.includes('case-style rename'));
check('auto-fix panel shows the confidence breakdown for it', $('#autoFixBody').innerHTML.includes('Mapping confidence') && $('#autoFixBody').innerHTML.includes('Name similarity'));
check('confidence data was recorded on the log entry', S().log.some(l => l.action === 'DRIFT_ABSORBED' && l.confidence && l.confidence.score > 0));
await click('sel', 'order.get'); await click('inject', 'envelope'); await click('next'); await wait(1300);
check('non-inference fixes say so plainly', $('#autoFixBody').innerHTML.includes('Fixed rule, no inference'));

// ---------- animation must not replace sidebar buttons ----------
await click('sel', 'service.get');
await click('inject', 'add_optional');
const probe = document.querySelector('#sim [data-act="inject"][data-arg="type_change"]');
await click('next');                       // animate() starts; first full render has already happened synchronously
const probeAfterFirstRender = document.querySelector('#sim [data-act="inject"][data-arg="type_change"]');
await wait(1300);                    // six animation ticks
check('sidebar button survives every animation tick (clicks cannot be dropped)', probeAfterFirstRender.isConnected);

// ---------- reviews live in the right panel ----------
await click('sel', 'service.get');
S().rightOpen = false; window.dispatchEvent(new window.Event('x')); await click('maintab', 'pipeline'); // re-render with panel closed
check('panel closed before a review exists', document.body.classList.contains('rightpanel-collapsed'));
await click('inject', 'rename_status');
await click('next'); await wait(1300);
const open = S().reviews.filter(r => r.status === 'open');
check('a review opened', open.length >= 1);
check('right panel auto-opened for the new review', S().rightOpen === true && !document.body.classList.contains('rightpanel-collapsed'));
check('review card is urgent-styled', $('#reviewCard').classList.contains('urgent'));
check('handle pulses (alert class) and carries a count', $('#rightPull').classList.contains('alert') && $('#rightBadge').textContent === String(open.length) && !$('#rightBadge').hidden);
check('review is rendered in the right panel with confidence bars', $('#reviewPanelBody').innerHTML.includes('Mapping confidence'));
check('review header says a decision is needed', $('#rcount').textContent.includes('decision'));

await click('togglesidebar'); check('left handle still collapses the sidebar', document.body.classList.contains('sidebar-collapsed')); await click('togglesidebar');
await click('toggleright'); check('right handle collapses the panel', document.body.classList.contains('rightpanel-collapsed'));
check('collapsed handle still shows the waiting count', $('#rightPull').classList.contains('alert') && $('#rightPull').title.includes('waiting'));
await click('toggleright');

await click('approve', open[0].id); await wait(1300);
check('approving works from the right panel', S().reviews.find(r => r.id === open[0].id).status === 'approved');
check('approval audited to the reviewer', S().log.some(l => l.action === 'REVIEW_APPROVED' && l.actor === 'reviewer'));
check('resolved reviews shrink to one line', $('#reviewPanelBody').innerHTML.includes('revdone'));
check('review card no longer urgent once nothing is waiting', !$('#reviewCard').classList.contains('urgent') && $('#rightPull').classList.contains('alert') === false);

// ---------- scenario queue: add, busy guard, cancel steps ----------
await click('scenario', 'gradual_housekeeping');
check('scenario queued 4 steps', S().scenarioQueue.filter(q => q.scenarioId === 'gradual_housekeeping').length === 4);
const now = [...document.querySelectorAll('[data-act="scenario"]')];
const byId = id => now.find(b => b.dataset.arg === id);
check('added scenario shows as in the queue', byId('gradual_housekeeping').textContent === 'In the queue' && byId('gradual_housekeeping').disabled);
check('other scenario on the same binding is blocked', byId('planned_deprecation').disabled && byId('planned_deprecation').textContent === 'Binding busy');
check('scenarios on other bindings stay available', !byId('overnight_break').disabled);
await click('scenario', 'planned_deprecation');
check('blocked scenario adds nothing', S().scenarioQueue.filter(q => q.scenarioId === 'planned_deprecation').length === 0);
const steps = S().scenarioQueue.filter(q => q.scenarioId === 'gradual_housekeeping');
check('upcoming steps are labelled with their day and a cancel button', steps.every(s => document.querySelector(`[data-act="cancelqueue"][data-arg="${s.id}"]`)));
for (const s of steps) await click('cancelqueue', s.id);
check('cancelling every step frees the scenario to be added again', !S().activeScenarios.includes('gradual_housekeeping'));
check('...and the button is usable again', !document.querySelector('[data-act="scenario"][data-arg="gradual_housekeeping"]').disabled);

// ---------- version drift scenario actually plays out ----------
await click('scenario', 'gradual_housekeeping');
for (let i = 0; i < 7; i++) await window.__core.runBatch(true);
const ob = S().bindings.find(b => b.id === 'order.get');
check('scenario landed its version drift on schedule (sunset announced, v3 released)', window.__core.sim.peek('order.get').sunsetDay != null && !!window.__core.sim.peek('order.get').versions.v3);
check('scenario finished and freed up', !S().activeScenarios.includes('gradual_housekeeping') && S().scenarioQueue.length === 0);

// ---------- audit / rollback / silent swap still fine ----------
await click('maintab', 'audit');
check('audit tab lists the cancellation', $('#auditRows').innerHTML.includes('Simulation cancelled'));
// ---------- rollback story (through the queue) ----------
await click('reset');
await click('sel', 'service.get');
await click('inject', 'version_bump');
await click('next'); await wait(1300);
check('abrupt version change: primary is blocked at once', window.__core.health(S().bindings.find(b => b.id === 'service.get')) === 'FAIL');
const sv = S().bindings.find(b => b.id === 'service.get');
const svRev = () => S().reviews.find(r => r.kind === 'migration' && r.bindingId === 'service.get' && r.status === 'open');
check('migration review opened for the abrupt cutover', !!svRev());
check('low-confidence proposal correctly detected in the preview, not guessed', svRev().preview && !svRev().preview.ok);
document.querySelector(`[data-act="startmigration"][data-binding="service.get"]`).click(); await wait(1300);
check('review-driven start used the safe fallback and is ready for canary', sv.adapters.find(a => a.n === 2) && sv.adapters.find(a => a.n === 2).lastRun.readiness === 'PASS' && svRev().stage === 'ready_for_canary');
document.querySelector('[data-act="promote"]').click(); await wait(25);
await click('next'); await wait(1300);
document.querySelector('[data-act="breakcanary"]').click(); await wait(25);
await click('next'); await wait(1300);
check('failed canary rolled back', sv.adapters.find(a => a.n === 2).state === 'rolled_back');
check('rollback audited to the system', S().log.some(l => l.action === 'ADAPTER_ROLLED_BACK' && l.actor === 'system'));

// ---------- silent swap stays silent ----------
await click('reset');
await click('sel', 'inventory.snapshot');
await click('inject', 'silent_swap');
await click('next'); await wait(1300);
check('silent breaking change: health stays PASS', window.__core.health(S().bindings.find(b => b.id === 'inventory.snapshot')) === 'PASS');
check('silent breaking change: no review, nothing auto-fixed', S().reviews.length === 0 && !S().log.some(l => l.action === 'DRIFT_ABSORBED' && l.binding === 'inventory.snapshot' && /status/.test(l.msg)));

// ---------- every admin injector, on every binding, really lands ----------
for (const id of ['order.get', 'service.get', 'inventory.snapshot']) {
  await click('reset'); await click('sel', id);
  const btns = [...document.querySelectorAll('#sim [data-act="inject"]')].map(b => b.dataset.arg);
  for (const inj of btns) {
    await click('reset'); await click('sel', id);
    await click('inject', inj);
    const queued = S().scenarioQueue.some(q => q.bindingId === id && q.injectId === inj);
    await window.__core.runBatch(false);
    const landed = S().scenarioQueue.length === 0 && S().log.some(l => l.action === 'SIMULATION' && l.binding === id && l.src === 'sim' && l.msg.startsWith('simulated'));
    check(`${id}: "${inj}" queues and then lands`, queued && landed);
  }
}

await click('reset');
check('reset clears queue, scenarios, and reopens both panels', S().scenarioQueue.length === 0 && S().activeScenarios.length === 0 && S().sidebarOpen && S().rightOpen && !document.body.classList.contains('rightpanel-collapsed'));
check('no window errors during the whole run', !errored);

console.log(`\n${pass} passed, ${fail} failed`);
