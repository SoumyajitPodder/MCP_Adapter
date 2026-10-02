// Shared helpers for writing connectors.

import { flatten } from '../utils.js';

// One line of delimited text -> fields. Handles quoted fields and escaped
// quotes; does not handle a quoted field that spans several lines.
export function splitLine(line, delimiter) {
  const out = []; let cur = '', quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else quoted = false; }
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === delimiter) { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

// Delimited text -> { records, columns, format }, or { unreadable: true } if
// the header doesn't split into at least two columns with this delimiter.
// Values stay strings: a CSV has no types, so none are invented.
export function parseDelimited(text, delimiter = ',') {
  const lines = String(text).split(/\r?\n/);
  const columns = splitLine(lines[0] || '', delimiter);
  const format = { delimiter };
  if (columns.length < 2) return { unreadable: true, format };
  const records = lines.slice(1).filter(Boolean).map(l => {
    const cells = splitLine(l, delimiter), o = {};
    columns.forEach((h, i) => { o[h] = cells[i]; });
    return o;
  });
  return { records, columns, format };
}

// JSON items (objects) -> flat records with dot-path keys.
export const toRecords = items => (Array.isArray(items) ? items : [items]).filter(x => x && typeof x === 'object').map(x => flatten(x));

// Reads body[path] for a dot path like "data.items"; the whole body if no path.
export function pick(body, path) {
  if (!path) return body;
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), body);
}

// Normalizes values a database driver may return into JSON-friendly ones.
export function plainValue(v) {
  if (v instanceof Date) return v.toISOString();
  if (typeof v === 'bigint') return Number(v);
  if (v && typeof v === 'object' && typeof v.toString === 'function' && v.constructor && v.constructor.name === 'Buffer') return v.toString('utf8');
  return v;
}

// binding.source may describe one version ({ ...spec }) or several
// ({ versions: { v2: spec, v3: spec } }); this returns the version map.
export const versionsOf = src => (src && src.versions) ? src.versions : { [(src && src.version) || 'v1']: src || {} };
