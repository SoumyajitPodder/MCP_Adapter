// Tiny DOM helpers shared by render.js and events.js.

export const $ = s => document.querySelector(s);

export const esc = s => String(s).replace(/[&<>"]/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'
}[c]));
