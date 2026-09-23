// Orchestration: this is the module that runs the six-stage sanity
// pipeline, opens and resolves reviews for ambiguous drift, promotes
// adapters and contracts through their lifecycle states, and serves the
// live-call runtime path. It ties detector + classifier + validator +
// adapters + the simulated upstream together into the behavior described
// in the Phase 1 LLD.

import { state, CONTRACTS, log, logDrift } from '../state.js';
import { fetchUpstream } from '../simulation/upstreams.js';
import { analyzeRaw, recordsFor, detect } from './detector.js';
import { scoreCandidate, classify } from './classifier.js';
import { translate, validateCanon, compareLKG } from './validator.js';
import { makeAdapter, rebaseline, deriveMapping, contractEnum } from './adapters.js';
import { primary, clone, SEV, pathsOf, stripPrefix, nameSim, inferFmt } from './utils.js';

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

// Sandbox replay + shadow comparison for a candidate mapping: is every
// sampled record well-formed against the canonical contract, and does it
// match last-known-good for records we already have ground truth for?
export function evalCandidate(b, a, cand) {
  const s = fetchUpstream(b, a.upstreamVersion, 6);
  if (s.status !== 200) return { sandbox: { total: 0, valid: 0, note: 'upstream unavailable' }, shadow: { compared: 0, identical: 0, skipped: 0, diffs: [] } };
  const recs = recordsFor(b, s, cand.unwrap, a.baseline.delimiter);
  const t = translate(cand, recs, { coerce: cand.coerce });
  const validOnes = t.out.filter(o => validateCanon(b, [o]).bad === 0);
  return {
    sandbox: { total: recs.length, valid: validOnes.length, note: t.errs[0] ? t.errs[0].detail : '' },
    shadow: compareLKG(b, a.lkg, validOnes)
  };
}

/* ---- new-version proposal (the API deprecation / migration path) ----
   Given the old mapping and a response from the new upstream version,
   scores every old-field-to-new-path pairing and keeps only confident
   matches. Anything left unresolved is reported rather than guessed. */
export function proposeVersion(b, oldMapping, s) {
  const raw = analyzeRaw(b, s, ',');
  let records = raw.records, unwrap = null;
  if (b.kind === 'REST') {
    const tops = new Set(Object.keys(records[0] || {}).map(p => p.split('.')[0]));
    if (tops.size === 1) {
      const k = [...tops][0];
      if (records.every(r => Object.keys(r).every(p => p.startsWith(k + '.')))) { unwrap = k; records = records.map(r => stripPrefix(r, k + '.')); }
    }
  }
  const paths = Object.keys(pathsOf(records));
  const pairs = [];
  oldMapping.fields.forEach(f => paths.forEach(p => pairs.push({ f, p, sc: scoreCandidate(f, p, records) })));
  pairs.sort((x, y) => y.sc - x.sc);
  const pickF = new Map(), usedP = new Set();
  pairs.forEach(x => { if (pickF.has(x.f.target) || usedP.has(x.p)) return; if (x.sc >= 0.6) { pickF.set(x.f.target, x); usedP.add(x.p); } });
  const m = clone(oldMapping); m.version = 1; m.unwrap = unwrap; m.coerce = false;
  const notes = [], unresolved = [];
  if (unwrap) notes.push(`unwrap envelope "${unwrap}"`);
  m.fields.forEach(f => {
    const pick = pickF.get(f.target);
    if (!pick) { const best = pairs.filter(x => x.f.target === f.target)[0]; unresolved.push(`${f.target}: no confident source (best guess "${best ? best.p : 'none'}" scored ${best ? best.sc : 0})`); return; }
    notes.push(`${f.target}: "${f.src}" → "${pick.p}" (confidence ${pick.sc})`); f.src = pick.p;
    if (f.transform === 'datetime') { const fm = inferFmt(records[0][f.src]); f.format = fm; notes.push(`${f.target}: datetime format ${fm}`); }
    if (f.transform === 'enum') {
      const opts = contractEnum(b, f.target);
      const vals = [...new Set(records.map(r => r[f.src]))];
      f.values = {};
      vals.forEach(v => {
        let best = null;
        opts.forEach(o => { const sc = nameSim(v, o); if (!best || sc > best.sc) best = { o, sc }; });
        if (best && best.sc >= 0.8) { f.values[v] = best.o; notes.push(`${f.target}: "${v}" → ${best.o}`); }
        else unresolved.push(`${f.target}: upstream value "${v}" has no confident canonical match`);
      });
    }
  });
  return { ok: unresolved.length === 0, mapping: m, notes, unresolved };
}

// detect + classify + log every item + open a review if anything is
// ambiguous. This is what both the daily batch and a live call run.
export function assess(b, a, src) {
  const s = fetchUpstream(b, a.upstreamVersion, 5);
  let det, cls;
  try {
    det = detect(b, a, s);
    cls = classify(b, a, det);
  } catch (e) {
    det = { events: [{ type: 'DETECTOR_ERROR', detail: String(e), fp: 'ERR' }], records: [] };
    cls = { items: [{ event: det.events[0], cls: 'UNKNOWN', reason: 'detector error, blocked' }], overall: 'UNKNOWN', ctx: { coerce: false, alias: {} } };
  }
  cls.items.forEach(it => logDrift(b, a, it, src));

  let review = null;
  const ri = cls.items.filter(i => i.rename || i.enumEv || i.dateChange);
  if (ri.length) {
    const sig = ri.map(i => i.event.fp).sort().join('|');
    review = state.reviews.find(r => r.adapterId === a.id && r.sig === sig);
    if (!review) {
      state.reviews.filter(r => r.adapterId === a.id && r.status === 'open').forEach(r => r.status = 'superseded');
      const P = proposeFix(b, a, det, cls.items);
      review = {
        id: 'R' + (state.reviews.length + 1), bindingId: b.id, adapterId: a.id, sig, day: state.day, status: 'open',
        events: cls.items.filter(i => SEV[i.cls] >= 1 && !i.sunset).map(i => i.reason),
        candidate: P.mapping, changes: P.changes, choices: P.choices, conf: P.conf, src
      };
      review.eval = evalCandidate(b, a, withChoices(review));
      state.reviews.unshift(review); state.metrics.reviews++;
      log(b, 'REVIEW_REQUIRED', `review ${review.id} opened with a candidate mapping (sandbox ${review.eval.sandbox.valid}/${review.eval.sandbox.total})`, src);
    }
  }
  return { s, det, cls, review };
}

// Simulates "the new adapter is worse": a candidate that passed the
// sandbox check when it was standing up (or looked fine on an earlier
// canary run) can still carry a correctness bug that only shows up under
// continued running. This corrupts one record's status value after
// translation, so canonical validation and the last-known-good regression
// catch it in stage 5 — exactly as they would catch a real mapping bug.
// The smoke test (stage 4) still passes, since translate() itself
// succeeds; it's the deeper check that finds the problem.
function applyCanaryFault(b, t) {
  if (!t.out.length) return t;
  const out = t.out.map((o, i) => i === 0 ? Object.assign({}, o, { [b.canon[1]]: 'WRONG_' + o[b.canon[1]] }) : o);
  return { out, errs: t.errs };
}

// Marks a canary adapter to carry the fault above on its next run. Only
// works on an adapter currently in canary, and only once.
export function injectCanaryFault(b, a) {
  if (a.state !== 'canary' || a.fault) return;
  a.fault = true;
  log(b, 'SYSTEM', `simulated: injected a correctness bug into ${a.id.split('/')[1]} (canary) — will surface on the next run`, 'sim');
}

// Upstream versions nobody has an active (tested/canary/primary) adapter
// for yet — surfaced in the version-check stage so a new release doesn't
// go unnoticed.
export function untargeted(b) {
  return Object.keys(b.upstream.versions).filter(v => !b.adapters.some(x => x.upstreamVersion === v && ['tested', 'canary', 'primary'].includes(x.state)));
}

// The six-stage sanity pipeline described in the LLD:
// version check -> schema comparison -> adapter compatibility ->
// smoke test -> canonical validation -> readiness.
export function runPipeline(b, a, src = 'batch') {
  const c = CONTRACTS[b.tool];
  const A = assess(b, a, src);
  const { s, det, cls } = A;
  const stages = [];
  const S1 = ['SUNSET_ANNOUNCED', 'HTTP_GONE'];

  // 1. Version check
  {
    const lines = [`contract ${b.tool}@${c.version} is ${c.state}`, `upstream ${a.upstreamVersion}: HTTP ${s.status}`];
    if (s.headers.Sunset) lines.push(`Sunset header present: ${det.events.find(e => e.type === 'SUNSET_ANNOUNCED').detail}`);
    const un = untargeted(b);
    if (un.length && a.state === 'primary') lines.push(`newer upstream version discovered: ${un.join(', ')} (no adapter yet)`);
    const st = s.status !== 200 ? 'fail' : ((s.headers.Sunset || un.length) && a.state === 'primary' ? 'warn' : 'pass');
    stages.push({ name: 'Version check', status: st, lines });
  }

  // 2. Schema comparison
  {
    const evs = det.events.filter(e => !S1.includes(e.type));
    const lines = evs.length ? evs.map(e => e.detail) : ['no differences from the registered baseline'];
    if (s.status !== 200) {
      stages.push({ name: 'Schema comparison', status: 'skip', lines: ['skipped: upstream not readable'] });
    } else {
      const worst = cls.items.filter(i => !S1.includes(i.event.type)).reduce((m, i) => SEV[i.cls] > SEV[m] ? i.cls : m, 'COMPATIBLE');
      stages.push({ name: 'Schema comparison', status: worst === 'COMPATIBLE' ? 'pass' : worst === 'REVIEW_REQUIRED' ? 'warn' : 'fail', lines });
    }
  }

  // 3. Adapter compatibility
  {
    const lines = cls.items.length
      ? cls.items.map(i => `${i.cls === 'COMPATIBLE' ? 'absorb' : i.cls === 'REVIEW_REQUIRED' ? 'review' : 'block'}: ${i.reason}`)
      : ['baseline matches, nothing to absorb'];
    if (A.review) lines.push(`review ${A.review.id} is ${A.review.status}`);
    stages.push({ name: 'Adapter compatibility', status: cls.overall === 'COMPATIBLE' ? 'pass' : cls.overall === 'REVIEW_REQUIRED' ? 'warn' : 'fail', lines });
  }

  // 4. Smoke test
  let t = null, smokeOk = false;
  if (s.status !== 200 || !det.records.length) {
    stages.push({ name: 'Smoke test', status: 'skip', lines: ['skipped: nothing to translate'] });
  } else {
    t = translate(a.mapping, det.records, cls.ctx);
    if (a.fault) t = applyCanaryFault(b, t);
    smokeOk = t.errs.length === 0;
    const lines = [`${t.out.length}/${det.records.length} sample records translated`];
    t.errs.slice(0, 3).forEach(e => lines.push(`${e.code} on ${e.field}: ${e.detail}`));
    if (Object.keys(cls.ctx.alias).length || cls.ctx.coerce) lines.push('translated with deterministic absorption rules applied');
    stages.push({ name: 'Smoke test', status: smokeOk ? 'pass' : 'fail', lines });
  }

  // 5. Canonical validation + regression against last-known-good
  let valOk = false;
  if (!t) {
    stages.push({ name: 'Canonical validation', status: 'skip', lines: ['skipped'] });
  } else {
    const v = validateCanon(b, t.out);
    const cmp = compareLKG(b, a.lkg, t.out);
    const lines = [
      v.bad ? `${v.bad} field violations against ${b.tool}@${c.version}` : `all ${t.out.length} outputs satisfy ${b.tool}@${c.version}`,
      `regression vs last-known-good: ${cmp.identical}/${cmp.compared} identical${cmp.skipped ? `, ${cmp.skipped} new keys not comparable` : ''}`
    ];
    cmp.diffs.slice(0, 2).forEach(d => lines.push(`${d.key}.${d.field}: was ${d.was}, now ${d.now}`));
    const pr = primary(b);
    if (a.state !== 'primary' && pr && pr !== a) {
      const c2 = compareLKG(b, pr.lkg, t.out);
      lines.push(`shadow vs primary ${pr.id.split('/')[1]}: ${c2.identical}/${c2.compared} identical`);
      if (c2.diffs.length) { valOk = false; cmp.diffs.push(...c2.diffs); }
    }
    valOk = v.bad === 0 && cmp.diffs.length === 0 && t.out.length > 0;
    stages.push({ name: 'Canonical validation', status: valOk ? 'pass' : 'fail', lines });
  }

  // 6. Readiness
  let readiness; const reasons = [];
  if (cls.overall === 'COMPATIBLE' && smokeOk && valOk) { readiness = 'PASS'; reasons.push('all checks passed'); }
  else if (cls.overall === 'REVIEW_REQUIRED') { readiness = 'REVIEW'; reasons.push(A.review && A.review.status === 'rejected' ? 'review was rejected, migration needed' : 'waiting on human review or migration'); }
  else {
    readiness = 'FAIL';
    if (cls.overall === 'COMPATIBLE' && a.fault) reasons.push('candidate produced incorrect output not explained by any upstream drift; treated as a regression and blocked');
    else reasons.push(cls.overall === 'COMPATIBLE' ? 'unexplained failure, treated as UNKNOWN and blocked' : 'breaking or unknown change, traffic fails closed');
  }
  stages.push({ name: 'Readiness', status: readiness === 'PASS' ? 'pass' : readiness === 'REVIEW' ? 'warn' : 'fail', lines: reasons });

  const run = { day: state.day, adapterId: a.id, stages, overall: cls.overall, readiness };
  a.lastRun = run;
  if (a.state === 'canary' && readiness === 'PASS') {
    a.canaryPass++;
  } else if (a.state === 'canary' && readiness === 'FAIL') {
    // Rollback: the candidate never becomes primary. Whatever was already
    // serving stays exactly as it was — this is the whole point of a
    // canary stage.
    a.state = 'rolled_back';
    const p = primary(b);
    log(b, 'BREAKING', `${a.id.split('/')[1]} failed in canary and was rolled back automatically; ${p ? p.id.split('/')[1] + ' remains primary, unaffected' : 'no primary is currently serving'}`);
  }
  return run;
}

// A binding's overall health is the readiness of whatever is currently primary.
export function health(b) {
  if (CONTRACTS[b.tool].state === 'SUNSET') return 'SUNSET';
  const p = primary(b);
  return p && p.lastRun ? p.lastRun.readiness : 'PENDING';
}

// The daily proactive run: sunset any contract whose window has elapsed,
// then run the sanity pipeline against every non-frozen adapter, and
// record a one-line-per-binding summary (the "batch run overview").
export function runBatch(advance) {
  if (advance) state.day++;
  Object.entries(CONTRACTS).forEach(([t, c]) => {
    if (c.state === 'DEPRECATED' && c.sunsetDay != null && state.day >= c.sunsetDay) {
      c.state = 'SUNSET';
      log(null, 'SYSTEM', `contract ${t}@${c.version} reached SUNSET; tool is no longer served`);
    }
  });
  const results = [];
  state.bindings.forEach(b => {
    if (CONTRACTS[b.tool].state === 'SUNSET') { results.push({ bindingId: b.id, tool: b.tool, readiness: 'SUNSET' }); return; }
    b.adapters.filter(a => ['tested', 'canary', 'primary'].includes(a.state)).forEach(a => runPipeline(b, a));
    results.push({ bindingId: b.id, tool: b.tool, readiness: health(b) });
  });
  state.batches++;
  state.batchHistory.unshift({ n: state.batches, day: state.day, results, sourcesChecked: state.bindings.length });
  if (state.batchHistory.length > 20) state.batchHistory.pop();
  state.selBatch = null; // always land on the run that just completed
}

// The runtime path: same detector, same classifier, but fails closed
// instead of opening a review, since there's no time to wait on one.
export function liveCall(b) {
  const c = CONTRACTS[b.tool];
  if (c.state === 'SUNSET') return { error: { code: 'CONTRACT_SUNSET', message: `${b.tool}@${c.version} is sunset and no longer served` } };
  const a = primary(b);
  const A = assess(b, a, 'runtime');
  if (A.s.status !== 200) return { error: { code: 'UPSTREAM_UNAVAILABLE', status: A.s.status, class: A.cls.overall, message: 'upstream version is gone; traffic fails closed until a new adapter is primary' } };
  const s4 = fetchUpstream(b, a.upstreamVersion, 4);
  const det = detect(b, a, s4);
  const cls = classify(b, a, det);
  if (!det.records.length) return { error: { code: 'DRIFT_BLOCKED', class: cls.overall, message: 'response could not be parsed; fails closed', events: cls.items.map(i => i.reason) } };
  const t = translate(a.mapping, det.records, cls.ctx);
  const v = validateCanon(b, t.out);
  if (t.errs.length || v.bad) return { error: { code: 'DRIFT_BLOCKED', class: cls.overall, message: 'canonical output could not be produced; fails closed', detail: t.errs.slice(0, 2).map(e => `${e.code}: ${e.detail}`), review: A.review ? A.review.id : null } };
  const warnings = cls.items.filter(i => i.sunset).map(i => i.event.detail);
  return {
    data: t.out.slice(0, 2),
    _meta: {
      contract: `${b.tool}@${c.version}`,
      served_by: `${a.id.split('/')[1]} (mapping v${a.mapping.version}, upstream ${a.upstreamVersion})`,
      absorbed: cls.items.filter(i => i.tier === 'A' && i.cls === 'COMPATIBLE' && i.event.type !== 'FIELD_ADDED').map(i => i.reason),
      warnings
    }
  };
}

/* ---- adapter and contract lifecycle actions ---- */

export function promote(b, a) {
  if (a.state === 'tested') {
    if (!a.lastRun || a.lastRun.readiness !== 'PASS') return;
    a.state = 'canary'; a.canaryPass = 0;
    log(b, 'PASS', `${a.id.split('/')[1]} promoted tested → canary`);
  } else if (a.state === 'canary') {
    if (!a.lastRun || a.lastRun.readiness !== 'PASS' || a.canaryPass < 1) return;
    const old = primary(b);
    if (old) { old.state = 'deprecated'; log(b, 'REVIEW_REQUIRED', `${old.id.split('/')[1]} moved primary → deprecated`); }
    a.state = 'primary';
    log(b, 'PASS', `${a.id.split('/')[1]} promoted canary → primary`);
  }
}

export function retire(b, a) {
  if (a.state === 'deprecated') { a.state = 'retired'; log(b, 'SYSTEM', `${a.id.split('/')[1]} retired`); }
}

// Breaking-drift / migration path: builds a proposed mapping for the new
// upstream version and stands up a new adapter in "tested" if it's confident.
export function standUp(b, ver) {
  const s = fetchUpstream(b, ver, 8);
  if (s.status !== 200) { log(b, 'BREAKING', `cannot stand up adapter for ${ver}: HTTP ${s.status}`); return; }
  const P = proposeVersion(b, primary(b).mapping, s);
  if (!P.ok) { b.proposalFailed = { ver, unresolved: P.unresolved }; log(b, 'BREAKING', `no automatic mapping for ${ver}: ${P.unresolved.length} unresolved field${P.unresolved.length === 1 ? '' : 's'}; operator must author it`); return; }
  const a = makeAdapter(b, ver, P.mapping, 'tested', 'proposed automatically');
  b.adapters.push(a); b.proposalFailed = null; state.selAdapter = a.id;
  log(b, 'PASS', `adapter ${a.id.split('/')[1]} for ${ver} stood up in tested state (${P.notes.length} mapping decisions)`);
  runPipeline(b, a);
}

// Fallback when the automatic proposal isn't confident enough: an
// operator-authored 1:1 mapping goes through the same tests and gates.
export function operatorMap(b, ver) {
  const m = deriveMapping(b, b.upstream.versions[ver]);
  const a = makeAdapter(b, ver, m, 'tested', 'operator authored');
  b.adapters.push(a); b.proposalFailed = null; state.selAdapter = a.id;
  log(b, 'PASS', `operator-authored adapter ${a.id.split('/')[1]} for ${ver} stood up in tested state`);
  runPipeline(b, a);
}

// Approval gate: every reviewer choice made, sandbox fully valid, zero
// shadow differences. Only then does the mapping registry get the new entry.
export function approve(rv) {
  const b = state.bindings.find(x => x.id === rv.bindingId);
  const a = b.adapters.find(x => x.id === rv.adapterId);
  if (rv.status !== 'open' || rv.choices.some(c => !c.selected)) return;
  const cand = withChoices(rv);
  const ev = evalCandidate(b, a, cand);
  if (ev.sandbox.valid !== ev.sandbox.total || ev.shadow.diffs.length) return;
  cand.version = a.mapping.version + 1; a.mapping = cand;
  rebaseline(b, a); rv.status = 'approved'; state.metrics.approved++;
  log(b, 'PASS', `review ${rv.id} approved; mapping registry now holds ${a.id.split('/')[1]} mapping v${cand.version}`);
  runPipeline(b, a);
}

export function reject(rv) {
  if (rv.status !== 'open') return;
  rv.status = 'rejected';
  log(state.bindings.find(x => x.id === rv.bindingId), 'BREAKING', `review ${rv.id} rejected`);
}

export function deprecateContract(tool) {
  const c = CONTRACTS[tool];
  if (c.state !== 'ACTIVE') return;
  c.state = 'DEPRECATED'; c.sunsetDay = state.day + 5;
  log(null, 'REVIEW_REQUIRED', `contract ${tool}@${c.version} deprecated; sunset on day ${c.sunsetDay}`);
}
