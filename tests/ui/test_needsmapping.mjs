import { JSDOM } from 'jsdom';
import fs from 'fs';
const dom = new JSDOM(fs.readFileSync('./index.html', 'utf8'));
globalThis.window = dom.window; globalThis.document = dom.window.document;
window.matchMedia = () => ({ matches: false });
window.addEventListener('error', e => console.error('WINDOW ERROR', e.error && e.error.stack));
process.on('unhandledRejection', e => { console.error('UNHANDLED', e); process.exit(1); });
await import('../../js/app.js');
const { controller: ctl, sim, ui } = window.__core;
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const wait = ms => new Promise(r => setTimeout(r, ms));
const click = async (act, arg) => { const b = $$(`[data-act="${act}"]`).find(x => arg == null || x.dataset.arg === arg); if (!b) throw new Error('no button ' + act + ' ' + arg); b.click(); await wait(0); };
const pick = async (sel, value) => { sel.value = value; sel.dispatchEvent(new window.Event('change', { bubbles: true })); await wait(30); };
let pass = 0, fail = 0; const check = (l, c) => { c ? pass++ : (fail++, console.log('FAIL:', l)); };

// Make the simulated upstream behave like a real one: it cannot hand over a mapping.
const supplyMapping = sim.connector.operatorMapping;
delete sim.connector.operatorMapping;
await click('sel', 'service.get'); await click('inject', 'version_bump'); await click('next'); await wait(20);
const rv = () => ctl.state.reviews.find(r => r.kind === 'migration' && r.bindingId === 'service.get' && r.status === 'open');
const panel = () => $('#reviewPanelBody').innerHTML;
const startBtn = () => $('#reviewPanelBody [data-act="startmigration"]');

check('migration review opened and knows this upstream cannot supply a mapping', rv() && rv().stage === 'not_started' && rv().canFallback === false);
check('card asks a person which upstream field holds the status', panel().includes('data-act="choosesrc"') && panel().includes('which upstream field holds this'));
check('the misleading "safe fallback" wording is NOT shown', !panel().includes('safe fallback'));
check('start is disabled until the person has chosen', startBtn().disabled);
const srcSel = $('#reviewPanelBody select[data-act="choosesrc"]');
check('the best candidate is offered with its match percentage', [...srcSel.options].some(o => o.value === 'lifecycle_state' && /% match/.test(o.textContent)));

await pick(srcSel, 'lifecycle_state');
check('after choosing the field, the unfamiliar status codes are asked about', $$('#reviewPanelBody select[data-act="choosevalue"]').length === 3);
check('start is still disabled while the codes are open', startBtn().disabled);

const meaning = { LIVE: 'active', PAUSED: 'suspended', CLOSED: 'terminated' };
for (const [code, canon] of Object.entries(meaning)) {
  await pick($$('#reviewPanelBody select[data-act="choosevalue"]').find(s => s.dataset.key === 'status:' + code), canon);
}
check('start is enabled once every open question is answered', !startBtn().disabled);

await click('startmigration', 'v2'.replace('v2', rv().targetVersion)); await wait(30);
const b = ctl.binding('service.get'), cand = b.adapters[1];
check('candidate created from the person\'s choices', cand && cand.state === 'tested' && cand.note === 'proposed automatically');
check('it reads status from the field they chose', cand.mapping.fields.find(f => f.target === 'status').src === 'lifecycle_state');
check('the codes mean what they said', JSON.stringify(cand.mapping.fields.find(f => f.target === 'status').values) === JSON.stringify({ LIVE: 'active', PAUSED: 'suspended', CLOSED: 'terminated' }));
check('and it passes every check against the current version', cand.lastRun.readiness === 'PASS' && rv().stage === 'ready_for_canary');

await click('promote', cand.id); await click('next'); await wait(20); await click('promote', cand.id); await wait(20);
check('migration completes and v3 is served', rv() === undefined && (await ctl.liveCall('service.get'))._meta.served_by.startsWith('v3'));

// the explicit-fallback path (the simulator CAN supply a mapping) is unchanged
sim.connector.operatorMapping = supplyMapping;
await window.__core.initState();
await click('sel', 'service.get'); await click('inject', 'version_bump'); await click('next'); await wait(20);
check('with a connector that can supply a mapping, the one-button fallback card is still shown', panel().includes('safe fallback') && !panel().includes('data-act="choosesrc"'));
console.log(`\n${pass} passed, ${fail} failed`);
