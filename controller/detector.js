// Detect stage: compares a normalized upstream fetch result against the
// baseline stored on its adapter and lists every difference it finds
// (fields added or removed, types changed, envelopes appearing, enum values
// it hasn't seen, date formats flipping, and so on). It does not decide
// whether any of this is safe — that judgment is the classifier's job.
//
// Pure: no state, no I/O. Time arrives through `clock`, and the shape of
// `s` is the connector contract (see connectors/README in controller/README.md),
// so this file never needs to know whether the bytes came from a REST API,
// a CSV drop, or a database table.

import { stripPrefix, pathsOf, inferFmt, DAY_MS } from './utils.js';

export const unwrapRecords = (records, unwrap) =>
  unwrap ? records.map(r => stripPrefix(r, unwrap + '.')) : records;

export function detect(b, a, s, clock) {
  const ev = [];

  if (s.status === 410) {
    ev.push({ type: 'HTTP_GONE', path: '', detail: `${a.upstreamVersion} returned 410 Gone`, fp: 'GONE:' + a.upstreamVersion });
    return { events: ev, records: [] };
  }
  // Any other non-200 is an availability problem, not a change in shape.
  // Treating an outage as drift would open migration reviews for a blip.
  if (s.status !== 200) {
    ev.push({ type: 'UPSTREAM_ERROR', path: '', detail: `${a.upstreamVersion} returned HTTP ${s.status}${s.error ? ' — ' + s.error : ''}`, fp: 'ERROR:' + s.status });
    return { events: ev, records: [] };
  }
  if (s.retirement) {
    const left = Math.ceil((s.retirement.at - clock.now()) / DAY_MS);
    ev.push({
      type: 'SUNSET_ANNOUNCED', path: '',
      detail: `${a.upstreamVersion} retires on ${clock.format(s.retirement.at)} (${left} day${left === 1 ? '' : 's'} left)`,
      fp: 'SUNSET:' + s.retirement.at
    });
  }

  if (s.unreadable) {
    const d = a.baseline && a.baseline.format && a.baseline.format.delimiter;
    ev.push({ type: 'FORMAT_UNREADABLE', path: '', detail: d ? `header cannot be parsed with delimiter "${d}"` : 'response body could not be parsed', fp: 'UNREADABLE' });
    return { events: ev, records: [] };
  }

  let records = s.records, wrapper = null;
  if (!s.columns) {
    // Document-style source (JSON): an envelope can appear around the payload.
    if (a.mapping.unwrap) {
      records = unwrapRecords(s.records, a.mapping.unwrap);
    } else {
      const tops = new Set(Object.keys(s.records[0] || {}).map(p => p.split('.')[0]));
      if (tops.size === 1) {
        const k = [...tops][0];
        const st = s.records.map(r => stripPrefix(r, k + '.'));
        if (a.mapping.fields.every(f => f.src in (st[0] || {}))) {
          wrapper = k; records = st;
          ev.push({ type: 'ENVELOPE_ADDED', path: k, detail: `response is now wrapped in "${k}"`, fp: 'ENVELOPE:' + k });
        }
      }
    }
  } else {
    // Tabular source (CSV, SQL): columns are read by name, so a reorder is harmless.
    const cb = (a.baseline && a.baseline.columns) || [], cn = s.columns;
    if (cb.join() !== cn.join() && [...cb].sort().join() === [...cn].sort().join()) {
      ev.push({ type: 'COLUMNS_REORDERED', path: '', detail: 'column order changed', fp: 'REORDER:' + cn.join() });
    }
  }

  const bp = a.baseline.paths, op = pathsOf(records);
  Object.keys(bp).forEach(p => { if (!(p in op)) ev.push({ type: 'FIELD_REMOVED', path: p, detail: `"${p}" is no longer present`, fp: 'REMOVED:' + p }); });
  Object.keys(op).forEach(p => {
    if (!(p in bp)) ev.push({ type: 'FIELD_ADDED', path: p, detail: `new field "${p}" (${op[p]})`, fp: 'ADDED:' + p });
    else if (op[p] !== bp[p] && op[p] !== 'null' && bp[p] !== 'null') ev.push({ type: 'TYPE_CHANGED', path: p, from: bp[p], to: op[p], detail: `"${p}" changed ${bp[p]} → ${op[p]}`, fp: `TYPE:${p}:${op[p]}` });
  });

  a.mapping.fields.forEach(f => {
    if (!(f.src in op)) return;
    const vals = records.map(r => r[f.src]).filter(v => v !== null);
    if (f.transform === 'enum') {
      [...new Set(vals.filter(v => v !== undefined && !(v in f.values)))].forEach(v =>
        ev.push({ type: 'ENUM_NEW', path: f.src, field: f.target, value: v, detail: `unmapped value "${v}" at "${f.src}"`, fp: `ENUM:${f.src}:${v}` }));
    }
    if ((f.transform === 'datetime' || f.transform === 'date') && vals.length) {
      const fm = inferFmt(vals[0]);
      if (fm !== f.format) ev.push({ type: 'DATE_FORMAT_CHANGED', path: f.src, field: f.target, from: f.format, to: fm, detail: `"${f.src}" format ${f.format} → ${fm}`, fp: `DATE:${f.src}:${fm}` });
    }
  });

  return { events: ev, records, wrapper };
}
