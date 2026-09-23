// Translation and validation: turns raw upstream records into the
// canonical shape using a mapping, checks the result against the
// canonical contract, and can diff a translation against last-known-good
// output for the same records (the regression check described in the LLD).

import { CONTRACTS } from '../state.js';
import { isoZ } from './utils.js';

export function parseDate(v, fmt) {
  if (typeof v !== 'string') throw { code: 'INVALID_DATE', detail: 'not a string' };
  if (fmt === 'US') {
    const m = /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2}):(\d{2})$/.exec(v);
    if (!m) throw { code: 'INVALID_DATE', detail: `"${v}" is not MM/DD/YYYY HH:MM:SS` };
    const d = new Date(Date.UTC(+m[3], +m[1] - 1, +m[2], +m[4], +m[5], +m[6]));
    if (d.getUTCMonth() !== +m[1] - 1) throw { code: 'INVALID_DATE', detail: `"${v}" is not a real date` };
    return isoZ(d);
  }
  if (fmt === 'ISO') {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(v)) throw { code: 'INVALID_DATE', detail: `"${v}" is not ISO-8601 UTC` };
    return v;
  }
  throw { code: 'INVALID_DATE', detail: 'unknown format ' + fmt };
}

// Executes a mapping against a set of raw records. `ctx.alias` lets an
// absorbed rename read from the new source field without editing the
// mapping itself; `ctx.coerce` lets an absorbed type change through.
export function translate(mapping, records, ctx) {
  const out = [], errs = [];
  const alias = ctx.alias || {};
  const coerce = ctx.coerce || mapping.coerce;
  records.forEach((rec, ri) => {
    const o = {}; let bad = false;
    mapping.fields.forEach(f => {
      const key = alias[f.src] || f.src;
      let v = rec[key];
      if (v === undefined || v === null || v === '') { errs.push({ ri, field: f.target, code: 'MISSING_REQUIRED', detail: `source "${key}" absent` }); bad = true; return; }
      try {
        if (f.transform === 'identity') {
          if (typeof v !== 'string') {
            if (typeof v === 'number' && coerce) v = String(v);
            else throw { code: 'TYPE_MISMATCH', detail: `${typeof v} at "${key}", string expected` };
          }
        } else if (f.transform === 'enum') {
          const m = f.values[v];
          if (m === undefined) throw { code: 'INVALID_ENUM', detail: `"${v}" has no mapping` };
          v = m;
        } else if (f.transform === 'datetime') {
          v = parseDate(v, f.format);
        }
        o[f.target] = v;
      } catch (e) {
        errs.push({ ri, field: f.target, code: e.code || 'TRANSLATION_FAILURE', detail: e.detail || String(e) });
        bad = true;
      }
    });
    if (!bad) out.push(o);
  });
  return { out, errs };
}

// Does a translated record satisfy the tool's canonical contract?
export function validateCanon(b, out) {
  const c = CONTRACTS[b.tool];
  let bad = 0; const msgs = [];
  out.forEach(o => {
    c.fields.forEach(f => {
      const v = o[f.name];
      let ok = typeof v === 'string' && v.length > 0;
      if (ok && f.type === 'enum') ok = f.values.includes(v);
      if (ok && f.type === 'datetime') ok = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(v);
      if (!ok) { bad++; msgs.push(`${f.name}=${JSON.stringify(v)}`); }
    });
  });
  return { bad, msgs };
}

// Diffs a set of outputs against a last-known-good map, keyed by the
// tool's natural key. This is the regression / shadow-comparison check —
// proving output is well-typed is not the same as proving it is correct.
export function compareLKG(b, lkg, outs) {
  const key = b.canon[0];
  let compared = 0, identical = 0, skipped = 0;
  const diffs = [];
  outs.forEach(o => {
    const ref = lkg[o[key]];
    if (!ref) { skipped++; return; }
    compared++;
    const badf = b.canon.filter(k => ref[k] !== o[k]);
    if (badf.length) diffs.push({ key: o[key], field: badf[0], was: ref[badf[0]], now: o[badf[0]] });
    else identical++;
  });
  return { compared, identical, skipped, diffs };
}
