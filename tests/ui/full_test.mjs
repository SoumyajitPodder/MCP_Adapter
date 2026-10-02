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
const $$ = s => [...document.querySelectorAll(s)];
const wait = ms => new Promise(r => setTimeout(r, ms));
const click = async (act, arg) => { const b = [...document.querySelectorAll(`[data-act="${act}"]`)].find(x => arg == null || x.dataset.arg === arg); if (!b) { console.log('NO BUTTON:', act, arg); return false; } b.click(); await new Promise(r => setTimeout(r, 0)); return true; };
let pass = 0, fail = 0;
const check = (label, cond) => { cond ? pass++ : (fail++, console.log('FAIL:', label)); };

console.log('=== basic boot, no errors yet ===');
check('boots without error', !errored);
check('window.__core present', typeof window.__core === 'object');
check('4 main tabs', document.querySelectorAll('.mtab').length === 4);
check('APIs tab exists', !!$('#mtab-apis'));

console.log('\n=== display names show up ===');
check('order.get shows "Order API" in bindings list', $('#bindings').innerHTML.includes('Order API'));
await click('maintab', 'pipeline');
check('Pipeline header shows display name', $('#tabPipeline').innerHTML.includes('Order API'));

console.log('\n=== full scenario -> migration review -> resolution flow ===');
await click('scenario', 'planned_deprecation');
for (let i = 0; i < 5; i++) await window.__core.runBatch(true);
const mrev = () => S().reviews.find(r => r.kind === 'migration' && r.bindingId === 'order.get' && r.status === 'open');
check('migration review opened', !!mrev());
check('migration review has a clear reason', mrev() && mrev().reasonText.includes('order.get'));
await click('maintab', 'pipeline'); await click('sel', 'order.get');
check('Pipeline shows the pointer to Reviews', $('#tabPipeline').innerHTML.includes('This API needs attention'));

await click('togglesidebar'); // no-op check, just ensure no crash interacting with sidebar mid-flow
await click('togglesidebar');

const startBtn = () => document.querySelector(`[data-act="startmigration"][data-binding="order.get"]`);
check('start-migration button present in review panel', !!startBtn());
check('confidence chart shown before starting', $('#reviewPanelBody').innerHTML.includes('Field mapping confidence'));
startBtn().click(); await wait(25);
await wait(1300);
check('candidate created after clicking start', S().bindings.find(b=>b.id==='order.get').adapters.length === 2);
check('review advanced to ready_for_canary', mrev() && mrev().stage === 'ready_for_canary');

const canaryBtn = () => $$('[data-act="promote"]')[0];
check('promote-to-canary button present', !!canaryBtn());
canaryBtn().click(); await wait(25);
await click('next'); await wait(1300);
check('review shows ready_for_primary after clean canary run', mrev() && mrev().stage === 'ready_for_primary');
const primaryBtn = () => $$('[data-act="promote"]')[0];
primaryBtn().click(); await wait(25);
check('review resolved after promotion', !mrev());
check('health restored', window.__core.health(S().bindings.find(b=>b.id==='order.get')) !== 'FAIL');

console.log(`\n${pass} passed, ${fail} failed so far`);

console.log('\n=== rollback via review panel, then retry ===');
await window.__core.initState(); // hard reset via console API
await click('sel', 'order.get');
await click('inject', 'sunset');
await click('next'); await wait(1300);
const mrevO = () => S().reviews.find(r => r.kind === 'migration' && r.bindingId === 'order.get' && r.status === 'open');
document.querySelector(`[data-act="startmigration"][data-binding="order.get"]`).click(); await wait(25);
await wait(1300);
document.querySelector('[data-act="promote"]').click(); await wait(25); // to canary
await click('next'); await wait(1300); // clean canary run
// simulate failure directly via console since the button lives in Pipeline tab, still present
const a2 = S().bindings.find(b=>b.id==='order.get').adapters.find(a=>a.n===2);
window.__core.injectCanaryFault(S().bindings.find(b=>b.id==='order.get'), a2);
await click('next'); await wait(1300);
check('review shows rolled_back stage', mrevO() && mrevO().stage === 'rolled_back');
check('review panel shows try-again button', $('#reviewPanelBody').innerHTML.includes('Try migration again'));
document.querySelector(`[data-act="startmigration"][data-binding="order.get"]`).click(); await wait(25);
await wait(1300);
check('a fresh adapter (a3) was created for retry', S().bindings.find(b=>b.id==='order.get').adapters.some(a=>a.n===3));
check('review tracks the new attempt', mrevO() && mrevO().adapterId === 'order.get/a3');

console.log('\n=== no-target (unfixable) case surfaces honestly ===');
await window.__core.initState();
await click('sel', 'inventory.snapshot');
await click('inject', 'remove_required');
await click('next'); await wait(1300);
const mrevI = () => S().reviews.find(r => r.kind === 'migration' && r.bindingId === 'inventory.snapshot' && r.status === 'open');
check('no-target review opened', mrevI() && mrevI().stage === 'no_target');
await click('maintab', 'pipeline'); await click('sel', 'inventory.snapshot');
console.log(document.querySelector('#reviewPanelBody') ? 'panel exists' : 'MISSING');
check('review panel shows unfixable messaging', $('#reviewPanelBody').innerHTML.includes("can't be fixed from here"));

console.log(`\n${pass} passed, ${fail} failed so far`);

console.log('\n=== add API flow ===');
await window.__core.initState();
await click('maintab', 'apis');
check('APIs tab shows the 3 default bindings', document.querySelectorAll('#apiRows tr').length === 3);

const nameInput = $('#apiNameInput'), kindInput = $('#apiKindInput');
nameInput.value = 'Billing API';
kindInput.value = 'REST';
await click('addapi');
check('new binding added to state', S().bindings.some(b => b.id === 'billing_api'));
check('APIs tab now shows 4 rows', document.querySelectorAll('#apiRows tr').length === 4);
check('success message shown', $('#addApiMsg').textContent.includes('Added'));
check('new binding auto-selected', S().sel === 'billing_api');

console.log('\n=== new API works with existing injectors immediately ===');
await click('sel', 'billing_api');
const billingInjectors = document.querySelectorAll('#sim [data-act="inject"]');
check('injectors render for the new API', billingInjectors.length > 0);
await click('inject', 'rename_case');
await click('next'); await wait(1300);
check('drift absorbed cleanly on the new API', S().log.some(l => l.binding === 'billing_api' && l.action === 'DRIFT_ABSORBED'));
check('health stays healthy', window.__core.health(S().bindings.find(b=>b.id==='billing_api')) === 'PASS');

console.log('\n=== new API supports a full version migration too ===');
await click('inject', 'sunset');
await click('next'); await wait(1300);
const bmrev = () => S().reviews.find(r => r.kind === 'migration' && r.bindingId === 'billing_api' && r.status === 'open');
check('migration review opens for custom API', !!bmrev());
document.querySelector(`[data-act="startmigration"][data-binding="billing_api"]`).click(); await wait(25);
await wait(1300);
check('preview was high-confidence (auto-derived shape)', bmrev().stage === 'ready_for_canary');
document.querySelector('[data-act="promote"]').click(); await wait(25);
await click('next'); await wait(1300);
document.querySelector('[data-act="promote"]').click(); await wait(25);
check('custom API migration resolves cleanly', !bmrev());
check('custom API now on v2', S().bindings.find(b=>b.id==='billing_api').adapters.find(a=>a.state==='primary').upstreamVersion === 'v2');

console.log('\n=== duplicate name / bad name rejected ===');
await click('maintab', 'apis');
nameInput.value = 'Billing API';
await click('addapi');
check('duplicate rejected', $('#addApiMsg').textContent.includes('already exists'));
nameInput.value = '';
await click('addapi');
check('empty name rejected', $('#addApiMsg').textContent.includes('name'));

console.log('\n=== remove API flow (requires confirm) ===');
const rows = () => document.querySelectorAll('#apiRows tr').length;
const removeBtn = () => [...document.querySelectorAll('[data-act="removeapi"]')].find(b => b.dataset.arg === 'billing_api');
removeBtn().click(); await wait(25);
check('first click asks to confirm, does not remove yet', rows() === 4 && removeBtn().textContent.includes('Confirm'));
removeBtn().click(); await wait(25);
check('second click actually removes it', rows() === 3 && !S().bindings.some(b => b.id === 'billing_api'));

console.log('\n=== cannot remove the last remaining API ===');
await window.__core.initState();
await click('maintab', 'apis');
for (const btn of [...document.querySelectorAll('[data-act="removeapi"]')]) { btn.click(); await wait(25); btn.click(); await wait(25); }
check('at least one API remains', S().bindings.length >= 1);

console.log(`\n${pass} passed, ${fail} failed so far`);

console.log('\n=== regression: no false "newer version" warning after a completed migration ===');
await window.__core.initState();
await click('sel', 'order.get'); await click('inject', 'sunset'); await click('next'); await wait(1300);
const migrateOrders = async () => {
  document.querySelector('[data-act="startmigration"][data-binding="order.get"]').click(); await wait(1300);
  document.querySelector('[data-act="promote"]').click(); await click('next'); await wait(1300);
  document.querySelector('[data-act="promote"]').click(); await click('next'); await wait(1300);
};
await migrateOrders();
const po = () => window.__core.primary(S().bindings.find(b => b.id === 'order.get'));
check('primary is now v3', po().upstreamVersion === 'v3');
check('Version check stage passes (no false warning)', po().lastRun.stages[0].status === 'pass');
check('no "newer upstream version" line mentioning the retired version', !po().lastRun.stages[0].lines.some(l => l.includes('newer upstream version')));
await click('maintab', 'pipeline');
check('Pipeline view agrees: no warn state on the version check', !$('#tabPipeline').innerHTML.includes('<span class="dot">!</span><span class="nm">Version check'));

console.log('\n=== regression: a binding can be migrated more than once (v2 -> v3 -> v4) ===');
check('sunset injector available again after a completed migration', !document.querySelector('#sim [data-act="inject"][data-arg="sunset"]').disabled);
await click('inject', 'sunset'); await click('next'); await wait(1300);
check('second migration review opened for v4', !!S().reviews.find(r => r.kind === 'migration' && r.bindingId === 'order.get' && r.status === 'open' && r.targetVersion === 'v4'));
await migrateOrders();
check('primary is now v4', po().upstreamVersion === 'v4');
check('v2 and v3 both retired from service, in order', S().bindings.find(b => b.id === 'order.get').adapters.filter(a => a.state === 'deprecated').length === 2);
check('Version check passes after the second migration too', po().lastRun.stages[0].status === 'pass');
check('live calls served by v4', ((await window.__core.liveCall(S().bindings.find(b => b.id === 'order.get')))._meta || {}).served_by?.includes('v4'));

console.log(`\n${pass} passed, ${fail} failed so far`);
