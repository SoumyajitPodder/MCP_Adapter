// Classify stage: takes what the detector found and decides, per item,
// whether it is COMPATIBLE (safe to absorb automatically), REVIEW_REQUIRED
// (needs a human decision), BREAKING (needs a new adapter version), or
// UNKNOWN (block, don't guess).
//
// This module is a pure function of its arguments — it does not read
// application state and does not touch the DOM.

import { norm, nameSim, inferFmt, SEV } from './utils.js';
import { canonicalValue } from './validator.js';

// Confidence score for "is `path` the field that replaced `f`", returned as
// the blended score plus its three components so the UI can show the
// breakdown, not just the final number:
//   score = 0.5 * name similarity + 0.2 * type match + 0.3 * value-shape match
//
// `ref` ({ lkg, keyOf }) lets the value component use the strongest evidence
// there is that a new field is a renamed old one: for records we already
// know, the candidate holds exactly what the old field held. Names like
// quantity_available -> qty_avail look dissimilar to a string metric, but
// 42 -> 42 for every known record is hard to explain any other way. This can
// only raise a score; where there is nothing to compare, nothing changes.
export function scoreCandidate(f, path, records, ref) {
  const name = Math.max(nameSim(f.src, path), nameSim(f.target, path));
  const vals = records.map(r => r[path]).filter(v => v !== undefined && v !== null);
  const want = { number: 'number', integer: 'number', boolean: 'boolean' }[f.transform] || 'string';
  const type = vals.length
    ? (vals.every(v => typeof v === want) ? 1 : (want === 'string' && vals.every(v => typeof v === 'number') && f.transform === 'identity' ? 0.7 : 0))
    : 0;
  let value = 0.5;
  if (f.transform === 'enum') {
    const known = [...Object.keys(f.values), ...Object.values(f.values)];
    value = vals.length ? vals.filter(v => known.some(k => nameSim(String(v), k) >= 0.8)).length / vals.length : 0;
  } else if (f.transform === 'datetime' || f.transform === 'date') {
    value = vals.length && vals.every(v => inferFmt(v) !== 'OTHER') ? 1 : 0;
  }
  let continuity = null;
  if (ref && ref.lkg && ref.keyOf) {
    let compared = 0, same = 0;
    for (const rec of records) {
      const key = ref.keyOf(rec), known = key !== undefined && ref.lkg[key];
      if (!known || rec[path] === undefined || rec[path] === null) continue;
      compared++;
      if (canonicalValue(f, rec, path) === known[f.target]) same++;
    }
    if (compared) { continuity = same / compared; value = Math.max(value, continuity); }
  }
  const score = +(0.5 * name + 0.2 * type + 0.3 * value).toFixed(2);
  return { score, name: +name.toFixed(2), type: +type.toFixed(2), value: +value.toFixed(2), ...(continuity !== null ? { continuity: +continuity.toFixed(2) } : {}) };
}

export function classify(b, a, det) {
  const items = [], ctx = { coerce: false, alias: {} };
  const evs = det.events, recs = det.records || [];
  const removed = evs.filter(e => e.type === 'FIELD_REMOVED');
  const added = evs.filter(e => e.type === 'FIELD_ADDED');
  const used = new Set();
  const bySrc = new Map(a.mapping.fields.map(f => [f.src, f]));
  // How to find a record's key in the current response, so a candidate's values can be
  // compared with what we already knew about that same record.
  const keyField = a.mapping.fields.find(x => x.target === b.canon[0]);
  const ref = { lkg: a.lkg, keyOf: rec => { if (!keyField) return undefined; const v = rec[ctx.alias[keyField.src] || keyField.src]; return v === undefined || v === null ? undefined : String(v); } };

  // A removed field is only "safe" if there's a confident replacement.
  removed.forEach(r => {
    const f = bySrc.get(r.path);
    if (!f) { items.push({ event: r, cls: 'COMPATIBLE', tier: 'A', reason: 'unmapped field removed, no impact' }); return; }
    let best = null;
    added.forEach(ad => { if (used.has(ad.path)) return; const sc = scoreCandidate(f, ad.path, recs, ref); if (!best || sc.score > best.sc.score) best = { ad, sc }; });
    if (best && norm(best.ad.path) === norm(r.path)) {
      used.add(best.ad.path); ctx.alias[r.path] = best.ad.path;
      items.push({
        event: r, cls: 'COMPATIBLE', tier: 'A',
        alias: { from: r.path, to: best.ad.path, score: best.sc.score, name: best.sc.name, type: best.sc.type, value: best.sc.value },
        reason: `case-style rename, aliased "${r.path}" → "${best.ad.path}"`
      });
    } else if (best && best.sc.score >= 0.6) {
      used.add(best.ad.path);
      items.push({
        event: r, cls: 'REVIEW_REQUIRED', target: f.target,
        rename: { from: r.path, to: best.ad.path, score: best.sc.score, name: best.sc.name, type: best.sc.type, value: best.sc.value },
        reason: `probable rename "${r.path}" → "${best.ad.path}" (confidence ${best.sc.score})`
      });
    } else if (f.optional) {
      items.push({ event: r, cls: 'COMPATIBLE', tier: 'A', reason: `optional field "${r.path}" is absent; it reads as null` });
    } else {
      items.push({ event: r, cls: 'BREAKING', reason: `required source "${r.path}" is gone and no confident replacement exists` });
    }
  });

  added.forEach(ad => { if (!used.has(ad.path)) items.push({ event: ad, cls: 'COMPATIBLE', tier: 'A', reason: 'new optional field, ignored (not in mapping)' }); });

  evs.forEach(e => {
    switch (e.type) {
      case 'FIELD_REMOVED': case 'FIELD_ADDED': break; // handled above
      case 'ENVELOPE_ADDED':
        items.push({ event: e, cls: 'COMPATIBLE', tier: 'A', unwrap: e.path, reason: `envelope "${e.path}" unwrapped deterministically` });
        break;
      case 'COLUMNS_REORDERED':
        items.push({ event: e, cls: 'COMPATIBLE', tier: 'A', reason: 'columns are read by header name, order is irrelevant' });
        break;
      case 'TYPE_CHANGED': {
        const f = bySrc.get(e.path);
        if (!f) items.push({ event: e, cls: 'COMPATIBLE', tier: 'A', reason: 'type change on unmapped field, ignored' });
        else if (f.transform === 'identity' && e.to === 'number') { ctx.coerce = true; items.push({ event: e, cls: 'COMPATIBLE', tier: 'A', coerce: true, reason: 'number coerced to string (supported transform)' }); }
        else items.push({ event: e, cls: 'BREAKING', reason: 'unsupported type change on a mapped field' });
        break;
      }
      case 'ENUM_NEW':
        items.push({ event: e, cls: 'REVIEW_REQUIRED', target: e.field, enumEv: { value: e.value }, reason: `new enum value "${e.value}" needs a reviewer decision` });
        break;
      case 'DATE_FORMAT_CHANGED':
        items.push({ event: e, cls: 'REVIEW_REQUIRED', target: e.field, dateChange: { from: e.from, to: e.to }, reason: `date format changed ${e.from} → ${e.to}; day/month order and timezone must be confirmed` });
        break;
      case 'SUNSET_ANNOUNCED':
        items.push({ event: e, cls: 'REVIEW_REQUIRED', sunset: true, reason: 'planned upstream retirement: stand up the next adapter version' });
        break;
      case 'HTTP_GONE':
        items.push({ event: e, cls: 'BREAKING', reason: 'upstream version is gone; migrate to a new adapter version' });
        break;
      case 'FORMAT_UNREADABLE':
        items.push({ event: e, cls: 'BREAKING', reason: 'file cannot be parsed; quarantine and escalate' });
        break;
      case 'UPSTREAM_ERROR':
        items.push({ event: e, cls: 'UNKNOWN', reason: 'upstream unavailable (an outage, not drift); it will be re-checked on the next run' });
        break;
      default:
        items.push({ event: e, cls: 'UNKNOWN', reason: 'unrecognised drift; blocked until a human classifies it' });
    }
  });

  const overall = items.reduce((m, i) => SEV[i.cls] > SEV[m] ? i.cls : m, 'COMPATIBLE');
  return { items, overall, ctx };
}
