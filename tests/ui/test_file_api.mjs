import { JSDOM } from 'jsdom';
import fs from 'fs';
const dom = new JSDOM(fs.readFileSync('./index.html', 'utf8'));
globalThis.window = dom.window; globalThis.document = dom.window.document;
window.matchMedia = () => ({ matches: false });
window.addEventListener('error', e => console.error('WINDOW ERROR', e.error && e.error.stack));
process.on('unhandledRejection', e => { console.error('UNHANDLED', e); process.exit(1); });
await import('../../js/app.js');
const S = () => window.__core.getState();
const $ = s => document.querySelector(s);
const click = async (act, arg) => { const b = [...document.querySelectorAll(`[data-act="${act}"]`)].find(x => arg == null || x.dataset.arg === arg); if (!b) { console.log('NO BUTTON:', act, arg); return false; } b.click(); await new Promise(r => setTimeout(r, 0)); return true; };
const wait = ms => new Promise(r => setTimeout(r, ms));
let pass=0, fail=0; const check=(l,c)=>{c?pass++:(fail++,console.log('FAIL:',l));};

await click('maintab', 'apis');
$('#apiNameInput').value = 'Shipments Feed';
$('#apiKindInput').value = 'FILE';
await click('addapi');
check('FILE binding created', S().bindings.find(b=>b.id==='shipments_feed' && b.kind==='FILE'));

await click('sel', 'shipments_feed');
const injIds = [...document.querySelectorAll('#sim [data-act="inject"]')].map(b=>b.dataset.arg);
check('FILE-only injectors present (reorder, delimiter)', injIds.includes('reorder') && injIds.includes('delimiter'));
check('REST-only injectors absent (envelope, sunset)', !injIds.includes('envelope') && !injIds.includes('sunset'));

await click('inject', 'reorder');
await click('next'); await wait(1300);
check('reorder absorbed on FILE kind', window.__core.health(S().bindings.find(b=>b.id==='shipments_feed')) === 'PASS');

await click('inject', 'silent_swap');
await click('next'); await wait(1300);
check('silent swap on custom FILE API stays undetected', window.__core.health(S().bindings.find(b=>b.id==='shipments_feed')) === 'PASS');
check('no review opened for the silent swap', !S().reviews.some(r=>r.bindingId==='shipments_feed'));

console.log(`\n${pass} passed, ${fail} failed`);
