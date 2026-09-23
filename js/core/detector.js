// Detect stage: compares an upstream response against the baseline stored
// on its adapter and lists every difference it finds (fields added or
// removed, types changed, envelopes appearing, enum values it hasn't seen,
// date formats flipping, and so on). It does not decide whether any of
// this is safe — that judgment is the classifier's job (classifier.js).

import { state } from '../state.js';
import { flatten, stripPrefix, pathsOf, inferFmt } from './utils.js';

// Turns a raw upstream response (REST JSON items, or file text) into a flat
// list of records, plus the file's column header when relevant.
export function analyzeRaw(b, s, delim) {
  if (b.kind === 'FILE') {
    const lines = s.text.split('\n');
    const header = lines[0].split(delim);
    if (header.length < 2) return { unreadable: true };
    const records = lines.slice(1).filter(Boolean).map(l => {
      const c = l.split(delim);
      const o = {};
      header.forEach((h, i) => o[h] = c[i]);
      return o;
    });
    return { records, columns: header };
  }
  return { records: s.items.map(x => flatten(x)) };
}

// analyzeRaw plus an optional envelope-unwrap, used wherever a mapping
// (not the baseline) needs the records ready to translate.
export function recordsFor(b, s, unwrap, delim) {
  const r = analyzeRaw(b, s, delim);
  if (r.unreadable) return [];
  return unwrap ? r.records.map(x => stripPrefix(x, unwrap + '.')) : r.records;
}

// The main detect step: baseline vs. current response, for one adapter.
export function detect(b, a, s) {
  const ev = [];

  if (s.status === 410) {
    ev.push({ type: 'HTTP_GONE', path: '', detail: `${a.upstreamVersion} returned 410 Gone`, fp: 'GONE:' + a.upstreamVersion });
    return { events: ev, records: [], stage: 1 };
  }
  if (s.headers.Sunset) {
    const left = b.upstream.sunsetDay - state.day;
    ev.push({ type: 'SUNSET_ANNOUNCED', path: '', detail: `${a.upstreamVersion} retires on day ${b.upstream.sunsetDay} (${left} day${left === 1 ? '' : 's'} left)`, fp: 'SUNSET:' + b.upstream.sunsetDay });
  }

  const delim = a.baseline.delimiter;
  const raw = analyzeRaw(b, s, delim);
  if (raw.unreadable) {
    ev.push({ type: 'FORMAT_UNREADABLE', path: '', detail: `header cannot be parsed with delimiter "${delim}"`, fp: 'UNREADABLE' });
    return { events: ev, records: [] };
  }

  let records = raw.records, wrapper = null;
  if (b.kind === 'REST') {
    if (a.mapping.unwrap) {
      records = raw.records.map(r => stripPrefix(r, a.mapping.unwrap + '.'));
    } else {
      const tops = new Set(Object.keys(raw.records[0] || {}).map(p => p.split('.')[0]));
      if (tops.size === 1) {
        const k = [...tops][0];
        const st = raw.records.map(r => stripPrefix(r, k + '.'));
        if (a.mapping.fields.every(f => f.src in (st[0] || {}))) {
          wrapper = k; records = st;
          ev.push({ type: 'ENVELOPE_ADDED', path: k, detail: `response is now wrapped in "${k}"`, fp: 'ENVELOPE:' + k });
        }
      }
    }
  } else {
    const cb = a.baseline.columns, cn = raw.columns;
    if (cb.join() !== cn.join() && [...cb].sort().join() === [...cn].sort().join()) {
      ev.push({ type: 'COLUMNS_REORDERED', path: '', detail: 'column order changed', fp: 'REORDER:' + cn.join() });
    }
  }

  const bp = a.baseline.paths, op = pathsOf(records);
  Object.keys(bp).forEach(p => { if (!(p in op)) ev.push({ type: 'FIELD_REMOVED', path: p, detail: `"${p}" is no longer present`, fp: 'REMOVED:' + p }); });
  Object.keys(op).forEach(p => {
    if (!(p in bp)) ev.push({ type: 'FIELD_ADDED', path: p, detail: `new field "${p}" (${op[p]})`, fp: 'ADDED:' + p });
    else if (op[p] !== bp[p]) ev.push({ type: 'TYPE_CHANGED', path: p, from: bp[p], to: op[p], detail: `"${p}" changed ${bp[p]} → ${op[p]}`, fp: `TYPE:${p}:${op[p]}` });
  });

  a.mapping.fields.forEach(f => {
    if (!(f.src in op)) return;
    const vals = records.map(r => r[f.src]);
    if (f.transform === 'enum') {
      [...new Set(vals.filter(v => v !== undefined && !(v in f.values)))].forEach(v =>
        ev.push({ type: 'ENUM_NEW', path: f.src, field: f.target, value: v, detail: `unmapped value "${v}" at "${f.src}"`, fp: `ENUM:${f.src}:${v}` }));
    }
    if (f.transform === 'datetime') {
      const fm = inferFmt(vals[0]);
      if (fm !== f.format) ev.push({ type: 'DATE_FORMAT_CHANGED', path: f.src, field: f.target, from: f.format, to: fm, detail: `"${f.src}" format ${f.format} → ${fm}`, fp: `DATE:${f.src}:${fm}` });
    }
  });

  return { events: ev, records, wrapper };
}
