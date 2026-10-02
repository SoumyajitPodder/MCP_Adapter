// Translation and validation: turns raw upstream records into the
// canonical shape using a mapping, checks the result against the
// canonical contract, and can diff a translation against last-known-good
// output for the same records (the regression check described in the LLD).
//
// Contract field types: string · enum · datetime · date · number · integer ·
// boolean, any of which may be `optional` (absent or null reads as null).

import { isoZ } from './utils.js';

const bad = (code, detail) => ({ code, detail });

function realDate(Y, Mo, D) {
  const d = new Date(Date.UTC(Y, Mo - 1, D));
  return d.getUTCFullYear() === Y && d.getUTCMonth() === Mo - 1 && d.getUTCDate() === D;
}

// -> canonical "YYYY-MM-DDTHH:MM:SSZ". ISO input may carry an explicit
// offset or fractional seconds (both are normalised: an offset is exact, so
// converting to UTC is not a guess). US format is MM/DD/YYYY HH:MM:SS.
export function parseDate(v, fmt) {
  if (typeof v !== 'string') throw bad('INVALID_DATE', 'not a string');
  if (fmt === 'US') {
    const m = /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2}):(\d{2})$/.exec(v);
    if (!m) throw bad('INVALID_DATE', `"${v}" is not MM/DD/YYYY HH:MM:SS`);
    const d = new Date(Date.UTC(+m[3], +m[1] - 1, +m[2], +m[4], +m[5], +m[6]));
    if (d.getUTCMonth() !== +m[1] - 1) throw bad('INVALID_DATE', `"${v}" is not a real date`);
    return isoZ(d);
  }
  if (fmt === 'ISO') {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(v);
    if (!m) throw bad('INVALID_DATE', `"${v}" is not ISO-8601 with a time and a zone`);
    const [Y, Mo, D, h, mi, s] = m.slice(1, 7).map(Number), z = m[7];
    if (h > 23 || mi > 59 || s > 59 || !realDate(Y, Mo, D)) throw bad('INVALID_DATE', `"${v}" is not a real date and time`);
    let ms = Date.UTC(Y, Mo - 1, D, h, mi, s);
    if (z !== 'Z') {
      const oh = +z.slice(1, 3), om = +z.slice(4, 6);
      if (oh > 23 || om > 59) throw bad('INVALID_DATE', `"${v}" has an impossible offset`);
      ms -= (z[0] === '-' ? -1 : 1) * (oh * 60 + om) * 60000;
    }
    return isoZ(new Date(ms));
  }
  throw bad('INVALID_DATE', 'unknown format ' + fmt);
}

// -> canonical "YYYY-MM-DD" for a date with no time of day.
export function parseDay(v, fmt) {
  const m = fmt === 'ISO_DATE' && typeof v === 'string' ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(v) : null;
  if (!m) throw bad('INVALID_DATE', `"${v}" is not YYYY-MM-DD`);
  if (!realDate(+m[1], +m[2], +m[3])) throw bad('INVALID_DATE', `"${v}" is not a real date`);
  return v;
}

// Executes a mapping against a set of raw records. `ctx.alias` lets an
// absorbed rename read from the new source field without editing the
// mapping itself; `ctx.coerce` lets an absorbed type change through.
export function translate(mapping, records, ctx) {
  const out = [], errs = [];
  const alias = ctx.alias || {};
  const coerce = ctx.coerce || mapping.coerce;
  records.forEach((rec, ri) => {
    const o = {}; let failed = false;
    mapping.fields.forEach(f => {
      const key = alias[f.src] || f.src;
      let v = rec[key];
      if (v === undefined || v === null || v === '') {
        if (f.optional) { o[f.target] = null; return; }
        errs.push({ ri, field: f.target, code: 'MISSING_REQUIRED', detail: `source "${key}" absent` }); failed = true; return;
      }
      try {
        switch (f.transform) {
          case 'identity':
            if (typeof v !== 'string') {
              if (typeof v === 'number' && coerce) v = String(v);
              else throw bad('TYPE_MISMATCH', `${typeof v} at "${key}", string expected`);
            }
            break;
          case 'enum': { const m = f.values[v]; if (m === undefined) throw bad('INVALID_ENUM', `"${v}" has no mapping`); v = m; break; }
          case 'datetime': v = parseDate(v, f.format); break;
          case 'date': v = parseDay(v, f.format); break;
          case 'number': if (typeof v !== 'number' || !Number.isFinite(v)) throw bad('TYPE_MISMATCH', `${typeof v} at "${key}", number expected`); break;
          case 'integer': if (!Number.isInteger(v)) throw bad('TYPE_MISMATCH', `${typeof v} at "${key}", integer expected`); break;
          case 'boolean': if (typeof v !== 'boolean') throw bad('TYPE_MISMATCH', `${typeof v} at "${key}", boolean expected`); break;
        }
        o[f.target] = v;
      } catch (e) {
        errs.push({ ri, field: f.target, code: e.code || 'TRANSLATION_FAILURE', detail: e.detail || String(e) });
        failed = true;
      }
    });
    if (!failed) out.push(o);
  });
  return { out, errs };
}

// What field `f` would produce from `rec[path]`, or undefined if it can't be translated.
// Used as evidence when judging whether a new field is a renamed old one.
export function canonicalValue(f, rec, path) {
  const t = translate({ fields: [{ ...f, src: path, optional: false }] }, [rec], {});
  return t.out.length ? t.out[0][f.target] : undefined;
}

const CHECK = {
  string: v => typeof v === 'string' && v.length > 0,
  datetime: v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(v),
  date: v => { const m = typeof v === 'string' && /^(\d{4})-(\d{2})-(\d{2})$/.exec(v); return !!m && realDate(+m[1], +m[2], +m[3]); },
  number: v => typeof v === 'number' && Number.isFinite(v),
  integer: v => Number.isInteger(v),
  boolean: v => typeof v === 'boolean'
};

// Does a translated record satisfy the tool's canonical contract?
export function validateCanon(b, out) {
  const c = b.contract;
  let nbad = 0; const msgs = [];
  out.forEach(o => {
    c.fields.forEach(f => {
      const v = o[f.name];
      if (f.optional && (v === null || v === undefined)) return;
      const ok = f.type === 'enum' ? (typeof v === 'string' && f.values.includes(v)) : (CHECK[f.type] || CHECK.string)(v);
      if (!ok) { nbad++; msgs.push(`${f.name}=${JSON.stringify(v)}`); }
    });
  });
  return { bad: nbad, msgs };
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
