// Proposal logic: turning "something changed" into a candidate mapping a
// person can approve. Pure functions of (binding, mapping, fetch result) —
// nothing here reads state, a clock, or an upstream.

import { clone, nameSim, inferFmt, pathsOf, stripPrefix } from './utils.js';
import { scoreCandidate } from './classifier.js';

// A field's allowed canonical enum values, from the binding's own contract.
export function contractEnum(b, target) {
  return b.contract.fields.find(f => f.name === target).values;
}

/* ---- propose a fix for drift on a live adapter ----
   Folds every absorbable (Tier A) change into the candidate mapping too,
   so a reviewer sees the whole picture rather than just the item that
   forced a review. Any enum value with no confident canonical match
   becomes an open choice for the reviewer instead of a guess. */
export function proposeFix(b, a, det, items) {
  const m = clone(a.mapping); const changes = [], choices = []; let conf = 1;
  if (det.wrapper && !m.unwrap) { m.unwrap = det.wrapper; changes.push(`unwrap envelope "${det.wrapper}"`); }
  items.forEach(it => {
    if (it.coerce) { m.coerce = true; changes.push('coerce numbers to strings for identity fields'); }
    if (it.alias) { const f = m.fields.find(x => x.src === it.alias.from); f.src = it.alias.to; changes.push(`${f.target}: source "${it.alias.from}" → "${it.alias.to}" (case style)`); }
    if (it.rename) { const f = m.fields.find(x => x.target === it.target); f.src = it.rename.to; conf = Math.min(conf, it.rename.score); changes.push(`${f.target}: source "${it.rename.from}" → "${it.rename.to}" (confidence ${it.rename.score})`); }
    if (it.dateChange) { const f = m.fields.find(x => x.target === it.target); f.format = it.dateChange.to; changes.push(`${f.target}: datetime format ${it.dateChange.from} → ${it.dateChange.to}, source timezone stays ${m.sourceTz}`); }
    if (it.enumEv) {
      const f = m.fields.find(x => x.target === it.target);
      const opts = contractEnum(b, it.target);
      let best = null;
      opts.forEach(o => { const s = nameSim(it.enumEv.value, o); if (!best || s > best.s) best = { o, s }; });
      if (best && best.s >= 0.8) { f.values[it.enumEv.value] = best.o; changes.push(`${f.target}: "${it.enumEv.value}" → ${best.o} (name match)`); }
      else { choices.push({ key: it.target + ':' + it.enumEv.value, target: it.target, value: it.enumEv.value, options: opts, selected: null }); changes.push(`${f.target}: "${it.enumEv.value}" → reviewer must choose`); }
    }
  });
  return { mapping: m, changes, choices, conf };
}

// Applies a reviewer's enum choices on top of a candidate mapping.
export function withChoices(rv) {
  const m = clone(rv.candidate);
  rv.choices.forEach(c => { if (c.selected) m.fields.find(f => f.target === c.target).values[c.value] = c.selected; });
  return m;
}

/* ---- new-version proposal (the API deprecation / migration path) ----
   Given the old mapping and a fetch result from the new version, scores
   every old-field-to-new-path pairing and keeps only confident matches.
   Anything left unresolved is reported rather than guessed.
   `forced` ({ target: sourcePath }) lets a person settle specific fields the
   scorer couldn't, and `forcedValues` ({ 'target:upstreamValue': canonical })
   what an unfamiliar status code means — those picks are used as given and
   flagged. Values still open are returned in `unresolvedValues`. */
export function proposeVersion(b, oldMapping, s, forced = {}, forcedValues = {}) {
  let records = s.records, unwrap = null;
  if (!s.columns) {
    const tops = new Set(Object.keys(records[0] || {}).map(p => p.split('.')[0]));
    if (tops.size === 1) {
      const k = [...tops][0];
      if (records.every(r => Object.keys(r).every(p => p.startsWith(k + '.')))) { unwrap = k; records = records.map(r => stripPrefix(r, k + '.')); }
    }
  }
  const paths = Object.keys(pathsOf(records));
  const pairs = [];
  oldMapping.fields.forEach(f => paths.forEach(p => pairs.push({ f, p, sc: scoreCandidate(f, p, records) })));
  pairs.sort((x, y) => y.sc.score - x.sc.score);
  const pickF = new Map(), usedP = new Set();
  Object.entries(forced).forEach(([target, path]) => {
    const x = pairs.find(q => q.f.target === target && q.p === path);
    if (x && !usedP.has(path)) { pickF.set(target, Object.assign({ forced: true }, x)); usedP.add(path); }
  });
  pairs.forEach(x => { if (pickF.has(x.f.target) || usedP.has(x.p)) return; if (x.sc.score >= 0.6) { pickF.set(x.f.target, x); usedP.add(x.p); } });
  const m = clone(oldMapping); m.version = 1; m.unwrap = unwrap; m.coerce = false;
  const notes = [], unresolved = [], unresolvedValues = [], fieldScores = [];
  if (unwrap) notes.push(`unwrap envelope "${unwrap}"`);
  m.fields.forEach(f => {
    const pick = pickF.get(f.target);
    const from = f.src;
    const candidates = pairs.filter(x => x.f.target === f.target).slice(0, 4).map(x => ({ path: x.p, score: x.sc.score }));
    if (!pick) {
      const best = pairs.filter(x => x.f.target === f.target)[0];
      unresolved.push(`${f.target}: no confident source (best guess "${best ? best.p : 'none'}" scored ${best ? best.sc.score : 0})`);
      fieldScores.push({ target: f.target, from, to: best ? best.p : null, resolved: false, score: best ? best.sc.score : 0, name: best ? best.sc.name : 0, type: best ? best.sc.type : 0, value: best ? best.sc.value : 0, candidates });
      return;
    }
    notes.push(`${f.target}: "${f.src}" → "${pick.p}" (${pick.forced ? 'confirmed by a person, ' : ''}confidence ${pick.sc.score})`); f.src = pick.p;
    fieldScores.push({ target: f.target, from, to: pick.p, resolved: true, forced: !!pick.forced, score: pick.sc.score, name: pick.sc.name, type: pick.sc.type, value: pick.sc.value, candidates });
    if (f.transform === 'datetime' || f.transform === 'date') { const fm = inferFmt(records.map(r => r[f.src]).find(v => v != null)); f.format = fm; notes.push(`${f.target}: datetime format ${fm}`); }
    if (f.transform === 'enum') {
      const opts = contractEnum(b, f.target);
      const vals = [...new Set(records.map(r => r[f.src]).filter(v => v != null))];
      f.values = {};
      vals.forEach(v => {
        const key = `${f.target}:${v}`;
        if (forcedValues[key] && opts.includes(forcedValues[key])) { f.values[v] = forcedValues[key]; notes.push(`${f.target}: "${v}" → ${forcedValues[key]} (confirmed by a person)`); return; }
        let best = null;
        opts.forEach(o => { const sc = nameSim(v, o); if (!best || sc > best.sc) best = { o, sc }; });
        if (best && best.sc >= 0.8) { f.values[v] = best.o; notes.push(`${f.target}: "${v}" → ${best.o}`); }
        else { unresolved.push(`${f.target}: upstream value "${v}" has no confident canonical match`); unresolvedValues.push({ key, target: f.target, value: v, options: opts }); }
      });
    }
  });
  return { ok: unresolved.length === 0, mapping: m, notes, unresolved, unresolvedValues, fieldScores };
}

// A mapping that assumes every canonical field is named the same upstream.
// Only a starting point for inferMapping — it exists so the same scorer
// that handles migrations can bootstrap a brand-new binding.
export function seedMapping(contract) {
  return {
    version: 1, unwrap: null, sourceTz: 'UTC', coerce: false,
    fields: contract.fields.map(f => {
      const m = { target: f.name, src: f.name };
      if (f.optional) m.optional = true;
      if (f.type === 'enum') { m.transform = 'enum'; m.values = Object.fromEntries(f.values.map(v => [v, v])); }
      else if (f.type === 'datetime') { m.transform = 'datetime'; m.format = 'ISO'; }
      else if (f.type === 'date') { m.transform = 'date'; m.format = 'ISO_DATE'; }
      else if (['number', 'integer', 'boolean'].includes(f.type)) m.transform = f.type;
      else m.transform = 'identity';
      return m;
    })
  };
}

// Infers an initial mapping from a live sample when none was supplied.
// Same rule as everywhere else: confident matches only, the rest reported.
//
// One allowance a migration does NOT get: an OPTIONAL field with no
// confident source is accepted here as "not present upstream yet" (it reads
// as null), because at registration there is no earlier name to have lost.
// In a migration the same gap would be silently dropped data, so there it
// stays unresolved and a person is asked.
export function inferMapping(b, s) {
  const P = proposeVersion(b, seedMapping(b.contract), s);
  if (P.ok) return P;
  const optional = new Set(b.contract.fields.filter(f => f.optional).map(f => f.name));
  const absent = P.fieldScores.filter(fs => !fs.resolved && optional.has(fs.target)).map(fs => fs.target);
  if (!absent.length) return P;
  const unresolved = P.unresolved.filter(u => !absent.some(t => u.startsWith(t + ':')));
  return { ...P, ok: unresolved.length === 0, unresolved, notes: [...P.notes, ...absent.map(t => `${t}: optional, not present upstream yet (reads null)`)] };
}
