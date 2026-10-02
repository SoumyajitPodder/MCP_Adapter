import { JSDOM } from 'jsdom';
import fs from 'fs';
const dom = new JSDOM(fs.readFileSync('./index.html', 'utf8'));
globalThis.window = dom.window; globalThis.document = dom.window.document;
window.matchMedia = () => ({ matches: false });
let errored = false;
window.addEventListener('error', e => { errored = true; console.error('WINDOW ERROR', e.error && e.error.stack); });
process.on('unhandledRejection', e => { console.error('UNHANDLED', e); process.exit(1); });
await import('../../js/app.js');
const click = (act, arg) => { const b = [...document.querySelectorAll(`[data-act="${act}"]`)].find(x => arg == null || x.dataset.arg === arg); if (b) b.click(); return !!b; };

['pipeline', 'apis', 'mapping', 'audit'].forEach(t => {
  click('maintab', t);
  console.log(t, '-> visible:', !document.querySelector(`#tab${t.charAt(0).toUpperCase()}${t.slice(1)}`).hidden, ', has content:', document.querySelector(`#tab${t.charAt(0).toUpperCase()}${t.slice(1)}`).innerHTML.length > 20 || t === 'apis');
});
console.log('APIs tab rows on cold start:', document.querySelectorAll('#apiRows tr').length);
console.log('\nany window errors:', errored);
