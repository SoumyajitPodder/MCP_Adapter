// The lifecycle controller engine, as a factory. Everything stateful —
// bindings, adapters, reviews, the audit trail, the batch history — lives
// inside one createController() instance, along with the connectors, the
// clock, and the hooks it was given. Nothing is a module-level singleton,
// so several controllers can coexist, and nothing here touches a DOM, a
// simulator, or a global.
//
// Public operations that change state (runBatch, beginMigration, promote,
// approve, ...) are serialized through a single queue: with real, slow
// upstreams, two overlapping operations on the same binding would
// otherwise interleave at every `await`. Read paths (liveCall, health,
// the pure queries) are not queued.

import { detect, unwrapRecords } from './detector.js';
import { classify } from './classifier.js';
import { translate, validateCanon, compareLKG } from './validator.js';
import { proposeFix, proposeVersion, withChoices, inferMapping } from './proposals.js';
import { primary, clone, SEV, pathsOf, versionLabel, DAY_MS } from './utils.js';
import { validateConnector } from './connectors/contract.js';

const ID_RE = /^[a-z][a-z0-9_.]{1,40}$/i;
const LIVE = ['tested', 'canary', 'primary'];
const SETTLED = ['tested', 'canary', 'primary', 'deprecated', 'retired'];

function realClock() {
  const start = Date.now();
  return { now: () => Date.now(), day: () => Math.floor((Date.now() - start) / DAY_MS), format: ms => new Date(ms).toISOString().slice(0, 10) };
}

export function createController(opts = {}) {
  const clock = opts.clock || realClock();
  const options = Object.assign({ fetchTimeoutMs: 15000, sampleSize: 5 }, opts.options);
  const connectors = new Map();
  const listeners = {};
  const hooks = { afterTranslate: [] };
  const fresh = () => ({
    seq: 0, batches: 0, log: [], reviews: [], metrics: { absorbed: 0, blocked: 0, reviews: 0, approved: 0 },
    bindings: [], batchHistory: [], reviewSeq: 0
  });
  const state = fresh();
  let chain = Promise.resolve();

  /* ------------------------------ plumbing ------------------------------ */

  function on(type, fn) {
    (listeners[type] = listeners[type] || []).push(fn);
    return () => { listeners[type] = listeners[type].filter(f => f !== fn); };
  }
  function emit(type, payload) {
    (listeners[type] || []).slice().forEach(fn => { try { fn(payload); } catch (e) { console.error(`[controller] listener for "${type}" threw:`, e); } });
  }
  function enqueue(fn) {
    const run = chain.then(() => fn());
    chain = run.catch(() => {});
    return run;
  }
  const binding = id => state.bindings.find(b => b.id === id) || null;
  const asBinding = x => (typeof x === 'string' ? binding(x) : x);
  const asReview = x => (typeof x === 'string' ? state.reviews.find(r => r.id === x) || null : x);

  function registerConnector(name, impl) {
    validateConnector(impl, name);
    connectors.set(name, impl);
    return impl;
  }
  function connectorFor(b) {
    const c = connectors.get(b.connector);
    if (!c) throw new Error(`no connector registered under "${b.connector}" (binding ${b.id})`);
    return c;
  }

  // Appends one line to the audit trail. `meta` carries who (actor), what
  // (action) and, for automatic mapping decisions, the confidence breakdown.
  function audit(b, cls, msg, src, meta) {
    state.seq++;
    state.log.unshift({
      seq: state.seq, at: clock.now(), day: clock.day(), binding: b ? b.id : 'system', cls, msg, src: src || 'batch',
      actor: (meta && meta.actor) || 'system', action: (meta && meta.action) || cls, confidence: (meta && meta.confidence) || null
    });
    if (state.log.length > 300) state.log.pop();
  }

  // Logs one classified drift item once per adapter (de-duplicated by
  // fingerprint) and updates the absorbed/blocked counters. An outage is
  // logged but never counted as a blocked *change*.
  function logDrift(b, a, it, src) {
    const fp = it.event.fp || it.event.type;
    if (a.seen[fp]) return;
    a.seen[fp] = 1;
    if (it.tier === 'A') state.metrics.absorbed++;
    else if (SEV[it.cls] >= 2 && it.event.type !== 'UPSTREAM_ERROR') state.metrics.blocked++;
    const action = it.cls === 'COMPATIBLE' ? 'DRIFT_ABSORBED' : (SEV[it.cls] >= 2 ? 'DRIFT_BLOCKED' : 'DRIFT_REVIEW');
    const confidence = it.alias && it.alias.score != null ? it.alias : null;
    audit(b, it.cls, `${versionLabel(b, a)}: ${it.event.type.toLowerCase().replace(/_/g, ' ')}, ${it.reason}`, src, { actor: 'system', action, confidence });
  }

  /* ------------------------------ upstream I/O ------------------------------ */

  const hintsOf = a => (a && a.baseline && a.baseline.format) || undefined;
  const outage = e => ({ status: 599, headers: {}, records: [], columns: null, retirement: null, format: null, unreadable: false, error: String((e && e.message) || e) });
  const normalizeFetch = r => {
    r = r || {};
    return {
      status: r.status == null ? 200 : r.status, headers: r.headers || {}, records: Array.isArray(r.records) ? r.records : [],
      columns: r.columns || null, retirement: r.retirement || null, format: r.format || null, unreadable: !!r.unreadable, error: r.error || null
    };
  };

  // Every upstream read goes through here: a connector that throws, hangs,
  // or returns something half-formed becomes an ordinary "unavailable"
  // result instead of an exception that takes the pipeline down.
  async function fetchFor(b, version, limit, hints, args) {
    let conn;
    try { conn = connectorFor(b); } catch (e) { return outage(e); }
    let timer;
    try {
      const timeout = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`upstream did not respond within ${options.fetchTimeoutMs}ms`)), options.fetchTimeoutMs); });
      return normalizeFetch(await Promise.race([Promise.resolve().then(() => conn.fetch(b, version, { limit, hints, args })), timeout]));
    } catch (e) { return outage(e); } finally { clearTimeout(timer); }
  }

  // Which versions does the upstream offer right now? A connector with no
  // listVersions is treated as a single fixed version.
  async function refreshVersions(b) {
    let versions = null;
    try { const c = connectorFor(b); if (typeof c.listVersions === 'function') versions = await c.listVersions(b); }
    catch { return; } // keep what we knew
    const p = primary(b);
    b.knownVersions = Array.isArray(versions) && versions.length ? versions : (p ? [p.upstreamVersion] : (b.knownVersions || []));
  }

  // Versions genuinely waiting for an adapter: nothing active points at
  // them and they haven't already been superseded. A version whose only
  // history is a rolled-back attempt is still waiting, on purpose — that is
  // what makes "try again" possible.
  const untargeted = b => (b.knownVersions || []).filter(v => !b.adapters.some(x => x.upstreamVersion === v && SETTLED.includes(x.state)));

  /* -------------------------------- adapters -------------------------------- */

  async function rebaseline(b, a) {
    const s = await fetchFor(b, a.upstreamVersion, 6, hintsOf(a));
    if (s.status !== 200 || s.unreadable) return;
    const recs = unwrapRecords(s.records, a.mapping.unwrap);
    a.baseline = { paths: pathsOf(recs), columns: s.columns || null, format: s.format || {} };
    const t = translate(a.mapping, recs, {});
    a.lkg = {};
    t.out.forEach(o => { a.lkg[o[b.canon[0]]] = o; });
  }

  async function makeAdapter(b, ver, mapping, state0, note) {
    const a = {
      id: b.id + '/a' + (b.adapters.length + 1), n: b.adapters.length + 1, state: state0, upstreamVersion: ver,
      mapping, note: note || '', createdAt: clock.now(), createdDay: clock.day(), lastRun: null, canaryPass: 0, seen: {}, baseline: null, lkg: {}
    };
    await rebaseline(b, a);
    return a;
  }

  // Sandbox replay + shadow comparison for a candidate mapping: is every
  // sampled record well-formed against the contract, and does it match
  // last-known-good for records we already have ground truth for?
  async function evalCandidate(b, a, cand) {
    const s = await fetchFor(b, a.upstreamVersion, 6, hintsOf(a));
    if (s.status !== 200 || s.unreadable) return { sandbox: { total: 0, valid: 0, note: 'upstream unavailable' }, shadow: { compared: 0, identical: 0, skipped: 0, diffs: [] } };
    const recs = unwrapRecords(s.records, cand.unwrap);
    const t = translate(cand, recs, { coerce: cand.coerce });
    const validOnes = t.out.filter(o => validateCanon(b, [o]).bad === 0);
    return {
      sandbox: { total: recs.length, valid: validOnes.length, note: t.errs[0] ? t.errs[0].detail : '' },
      shadow: compareLKG(b, a.lkg, validOnes)
    };
  }

  /* ------------------------------ detect + review ------------------------------ */

  // detect + classify + log every item + open a review if anything is
  // ambiguous. This is what both the daily batch and a live call run.
  async function assess(b, a, src, prefetched) {
    const s = prefetched || await fetchFor(b, a.upstreamVersion, options.sampleSize, hintsOf(a));
    let det, cls;
    try {
      det = detect(b, a, s, clock);
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
      // Only a LIVE review absorbs a recurrence: one still open, or one a person rejected while the
      // same change has stayed visible (so a rejection sticks instead of nagging every batch).
      // A review that was approved, resolved or superseded is history: the same change appearing
      // again is a new occurrence and needs a new review, not a pointer to a closed one.
      review = state.reviews.find(r => r.kind === "mapping" && r.adapterId === a.id && r.sig === sig && (r.status === "open" || (r.status === "rejected" && !r.gone)));
      if (review && review.status === 'open') {
        review.eval = await evalCandidate(b, a, withChoices(review)); // keep the sandbox numbers current
      } else if (!review) {
        state.reviews.filter(r => r.adapterId === a.id && r.status === 'open').forEach(r => { r.status = 'superseded'; });
        const P = proposeFix(b, a, det, cls.items);
        review = {
          id: 'R' + (++state.reviewSeq), kind: 'mapping', bindingId: b.id, adapterId: a.id, sig, at: clock.now(), day: clock.day(), status: 'open',
          events: cls.items.filter(i => SEV[i.cls] >= 1 && !i.sunset).map(i => i.reason),
          renames: ri.filter(i => i.rename).map(i => i.rename),
          candidate: P.mapping, changes: P.changes, choices: P.choices, conf: P.conf, src
        };
        review.eval = await evalCandidate(b, a, withChoices(review));
        state.reviews.unshift(review); state.metrics.reviews++;
        audit(b, 'REVIEW_REQUIRED', `review ${review.id} opened with a candidate mapping (sandbox ${review.eval.sandbox.valid}/${review.eval.sandbox.total})`, src, { actor: 'system', action: 'REVIEW_OPENED' });
        emit('review.opened', { review, binding: b });
      }
    } else if (src === 'batch' && s.status === 200 && !s.unreadable && s.records.length) {
      // The change that opened a review is no longer visible in a full sample: close it
      // rather than leave it waiting on a person forever. Never done from an outage or
      // from a single-record call, which cannot tell "gone" from "not visible here".
      state.reviews.filter(r => r.kind === 'mapping' && r.adapterId === a.id && r.status === 'open').forEach(r => {
        r.status = 'resolved';
        audit(b, 'PASS', `review ${r.id} closed: the upstream no longer shows this change`, src, { actor: 'system', action: 'REVIEW_RESOLVED' });
      });
      // A rejection only holds while the change stays visible; once it has gone, a return is new.
      state.reviews.filter(r => r.kind === 'mapping' && r.adapterId === a.id && r.status === 'rejected' && !r.gone).forEach(r => { r.gone = true; });
    }
    return { s, det, cls, review };
  }

  /* --------------------------- the six-stage pipeline --------------------------- */

  // version check -> schema comparison -> adapter compatibility ->
  // smoke test -> canonical validation -> readiness.
  async function runPipeline(b, a, src = 'batch') {
    const c = b.contract;
    const A = await assess(b, a, src);
    const { s, det, cls } = A;
    const stages = [];
    const S1 = ['SUNSET_ANNOUNCED', 'HTTP_GONE', 'UPSTREAM_ERROR'];
    const down = s.status !== 200 && s.status !== 410; // unavailable, as opposed to gone

    // 1. Version check
    {
      const lines = [`contract ${b.tool}@${c.version} is ${c.state}`, `upstream ${a.upstreamVersion}: HTTP ${s.status}${s.error ? ` (${s.error})` : ''}`];
      if (s.retirement) { const ev = det.events.find(e => e.type === 'SUNSET_ANNOUNCED'); if (ev) lines.push(`Retirement notice: ${ev.detail}`); }
      const un = untargeted(b);
      if (un.length && a.state === 'primary') lines.push(`newer upstream version discovered: ${un.join(', ')} (no adapter yet)`);
      const st = s.status !== 200 ? 'fail' : ((s.retirement || un.length) && a.state === 'primary' ? 'warn' : 'pass');
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
      for (const h of hooks.afterTranslate) { const r = h({ binding: b, adapter: a, result: t }); if (r) t = r; }
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
        lines.push(`shadow vs primary ${versionLabel(b, pr)}: ${c2.identical}/${c2.compared} identical`);
        if (c2.diffs.length) cmp.diffs.push(...c2.diffs);
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
      if (down) reasons.push(`upstream unavailable (HTTP ${s.status}); traffic fails closed until it recovers`);
      else if (cls.overall === 'COMPATIBLE') reasons.push('output failed validation or regression checks that no absorbed change explains; treated as a regression and blocked');
      else reasons.push('breaking or unknown change, traffic fails closed');
    }
    stages.push({ name: 'Readiness', status: readiness === 'PASS' ? 'pass' : readiness === 'REVIEW' ? 'warn' : 'fail', lines: reasons });

    const run = { at: clock.now(), day: clock.day(), adapterId: a.id, stages, overall: cls.overall, readiness, retirement: s.retirement || null };
    a.lastRun = run;
    if (a.state === 'canary' && readiness === 'PASS') {
      a.canaryPass++;
    } else if (a.state === 'canary' && readiness === 'FAIL' && !down) {
      // Rollback: the candidate never becomes primary. Whatever was already
      // serving stays exactly as it was — that is the whole point of a
      // canary stage. (A transient outage is NOT a reason to roll back.)
      a.state = 'rolled_back';
      const p = primary(b);
      audit(b, 'BREAKING', `${versionLabel(b, a)} failed in canary and was rolled back automatically; ${p ? versionLabel(b, p) + ' remains primary, unaffected' : 'no primary is currently serving'}`, 'batch', { actor: 'system', action: 'ADAPTER_ROLLED_BACK' });
    }
    return run;
  }

  // A binding's overall health is the readiness of whatever is currently primary.
  function health(x) {
    const b = asBinding(x);
    if (b.contract.state === 'SUNSET') return 'SUNSET';
    const p = primary(b);
    return p && p.lastRun ? p.lastRun.readiness : 'PENDING';
  }

  // The proactive run: sunset any contract whose window has elapsed, then
  // run the sanity pipeline against every non-frozen adapter, and record a
  // one-line-per-binding summary (the "batch run overview").
  async function runBatchInner() {
    const now = clock.now();
    state.bindings.forEach(b => {
      const c = b.contract;
      if (c.state === 'DEPRECATED' && c.sunsetAt != null && now >= c.sunsetAt) {
        c.state = 'SUNSET';
        audit(null, 'SYSTEM', `contract ${b.tool}@${c.version} reached SUNSET; tool is no longer served`, 'batch', { actor: 'system', action: 'CONTRACT_SUNSET' });
      }
    });
    const results = [];
    for (const b of state.bindings.slice()) {
      if (b.contract.state === 'SUNSET') { results.push({ bindingId: b.id, tool: b.tool, readiness: 'SUNSET' }); continue; }
      await refreshVersions(b);
      for (const a of b.adapters.filter(x => LIVE.includes(x.state))) await runPipeline(b, a);
      await assessMigrationReview(b);
      results.push({ bindingId: b.id, tool: b.tool, readiness: health(b) });
    }
    state.batches++;
    const entry = { n: state.batches, at: clock.now(), day: clock.day(), results, sourcesChecked: state.bindings.length };
    state.batchHistory.unshift(entry);
    if (state.batchHistory.length > 20) state.batchHistory.pop();
    emit('batch.completed', entry);
    return entry;
  }

  // The runtime path: same detector, same classifier, but fails closed
  // instead of opening a review, since there's no time to wait on one.
  // A call for ONE specific record (the agent asked for order 1003, not "a
  // couple of samples"). The arguments go to the connector, which is
  // responsible for using them safely. The outcome is read three ways:
  //   - "no such record" (404) and "bad arguments" (400/422) are ANSWERS about
  //     the request, so they are returned as-is and never logged as drift or
  //     as an outage;
  //   - anything else runs through the same detection as every other read, so
  //     drift seen at runtime is audited (and opens a review) immediately,
  //     while the call itself fails closed.
  async function callWithArgs(b, a, args) {
    const c = b.contract;
    const s = await fetchFor(b, a.upstreamVersion, 1, hintsOf(a), args);
    if (s.status === 404) return { error: { code: 'NOT_FOUND', status: 404, message: s.error || 'the upstream has no record for those arguments' } };
    if (s.status === 400 || s.status === 422) return { error: { code: 'BAD_REQUEST', status: s.status, message: s.error || 'the upstream rejected those arguments' } };
    const A = await assess(b, a, 'runtime', s);
    if (s.status !== 200) {
      const gone = s.status === 410;
      return { error: { code: 'UPSTREAM_UNAVAILABLE', status: s.status, class: A.cls.overall, message: gone ? 'upstream version is gone; traffic fails closed until a new adapter is primary' : `upstream unavailable (HTTP ${s.status}); traffic fails closed until it recovers` } };
    }
    if (!A.det.records.length) return { error: { code: 'DRIFT_BLOCKED', class: A.cls.overall, message: 'response could not be parsed; fails closed', events: A.cls.items.map(i => i.reason) } };
    const t = translate(a.mapping, A.det.records, A.cls.ctx);
    const v = validateCanon(b, t.out);
    if (t.errs.length || v.bad) return { error: { code: 'DRIFT_BLOCKED', class: A.cls.overall, message: 'canonical output could not be produced; fails closed', detail: t.errs.slice(0, 2).map(e => `${e.code}: ${e.detail}`), review: A.review ? A.review.id : null } };
    return {
      data: t.out,
      _meta: {
        contract: `${b.tool}@${c.version}`,
        served_by: `${versionLabel(b, a)} (mapping v${a.mapping.version})`,
        absorbed: A.cls.items.filter(i => i.tier === 'A' && i.cls === 'COMPATIBLE' && i.event.type !== 'FIELD_ADDED').map(i => i.reason),
        warnings: A.cls.items.filter(i => i.sunset).map(i => i.event.detail)
      }
    };
  }

  async function liveCall(x, o = {}) {
    const b = asBinding(x);
    if (!b) return { error: { code: 'NO_SUCH_BINDING', message: `no such binding: ${x}` } };
    const c = b.contract;
    if (c.state === 'SUNSET') return { error: { code: 'CONTRACT_SUNSET', message: `${b.tool}@${c.version} is sunset and no longer served` } };
    const a = primary(b);
    if (!a) return { error: { code: 'NO_PRIMARY', message: `${b.tool} has no current adapter` } };
    if (o.args && Object.keys(o.args).length) return callWithArgs(b, a, o.args);
    const A = await assess(b, a, 'runtime');
    if (A.s.status !== 200) {
      const gone = A.s.status === 410 || A.s.status === 404;
      return { error: { code: 'UPSTREAM_UNAVAILABLE', status: A.s.status, class: A.cls.overall, message: gone ? 'upstream version is gone; traffic fails closed until a new adapter is primary' : `upstream unavailable (HTTP ${A.s.status}); traffic fails closed until it recovers` } };
    }
    const s4 = await fetchFor(b, a.upstreamVersion, 4, hintsOf(a));
    const det = detect(b, a, s4, clock);
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
        served_by: `${versionLabel(b, a)} (mapping v${a.mapping.version})`,
        absorbed: cls.items.filter(i => i.tier === 'A' && i.cls === 'COMPATIBLE' && i.event.type !== 'FIELD_ADDED').map(i => i.reason),
        warnings
      }
    };
  }

  /* ----------------------------- migration reviews -----------------------------
     A binding whose primary can no longer be trusted (an announced
     retirement, or a version that's already gone) needs a person to move it
     to a new version. That decision belongs in the same place every other
     decision shows up — the review list — with a plain-language reason, one
     clear recommended action at a time, and the mapping's confidence broken
     down the same way a probable rename's is. */

  function migrationReason(b, p) {
    const vstage = p && p.lastRun && p.lastRun.stages[0];
    if (vstage && vstage.status === 'fail') {
      return { key: 'gone', text: `The version ${p.upstreamVersion} currently used by ${b.tool} has stopped responding. It needs to be replaced before this tool can work again.` };
    }
    const ret = p && p.lastRun && p.lastRun.retirement;
    if (ret) {
      const left = Math.ceil((ret.at - clock.now()) / DAY_MS);
      return { key: 'sunset', text: left > 0
        ? `The version ${p.upstreamVersion} currently used by ${b.tool} is being retired in ${left} day${left === 1 ? '' : 's'}.`
        : `The version ${p.upstreamVersion} currently used by ${b.tool} was due to retire and may stop responding at any time.` };
    }
    return { key: 'breaking', text: `${b.tool} reported a change to its current version that could not be safely handled automatically.` };
  }

  // Creates, refreshes, or resolves the one open migration review for a
  // binding. "Needs attention" is judged from the current primary's own
  // last run (retiring, or already gone) plus whether an attempt is in
  // progress — never from "is there a version nothing points at", because
  // the old version becomes exactly that the moment it is safely deprecated.
  async function assessMigrationReview(x) {
    const b = asBinding(x);
    let review = state.reviews.find(r => r.kind === 'migration' && r.bindingId === b.id && r.status === 'open');
    const tracked = review && review.adapterId ? b.adapters.find(a => a.id === review.adapterId) : null;

    if (tracked && tracked.state === 'primary') { review.status = 'resolved'; review.stage = 'done'; return; }

    if (tracked && tracked.state === 'rolled_back' && !review.rolledBackNoted) {
      review.rolledBackNoted = true; review.attempts++; review.stage = 'rolled_back'; review.adapterId = null;
      return;
    }

    const active = (tracked && ['tested', 'canary'].includes(tracked.state))
      ? tracked
      : [...b.adapters].reverse().find(a => a.state === 'tested' || a.state === 'canary');

    const p = primary(b);
    // BREAKING covers both "the version is gone" and any other unrecoverable
    // change to the current version's shape (a required field disappearing).
    const primaryTroubled = !!(p && p.lastRun && (p.lastRun.overall === 'BREAKING' || p.lastRun.retirement));

    if (!primaryTroubled && !active) {
      if (review) { review.status = 'resolved'; review.stage = 'done'; }
      return;
    }

    // There may be nothing to migrate to at all — the upstream broke and
    // never offered a replacement version. That is still worth a card: it is
    // the "unfixable from here" case, not nothing to report.
    const targetVersion = active ? active.upstreamVersion : untargeted(b)[0];

    if (!review) {
      const reason = migrationReason(b, p);
      review = {
        id: 'M' + (++state.reviewSeq), kind: 'migration', bindingId: b.id, status: 'open', at: clock.now(), day: clock.day(),
        targetVersion: targetVersion || null, reasonKey: reason.key, reasonText: reason.text,
        adapterId: null, attempts: 0, rolledBackNoted: false
      };
      state.reviews.unshift(review);
      audit(b, 'REVIEW_REQUIRED', `migration needed: ${reason.text}`, 'batch', { actor: 'system', action: 'REVIEW_OPENED' });
      emit('review.opened', { review, binding: b });
    }

    if (targetVersion) review.targetVersion = targetVersion;

    if (active) {
      review.adapterId = active.id;
      if (active.state === 'tested') {
        review.stage = (active.lastRun && active.lastRun.readiness === 'PASS') ? 'ready_for_canary' : 'checking';
      } else {
        const clean = active.canaryPass >= 1 && active.lastRun && active.lastRun.readiness === 'PASS';
        review.stage = clean ? 'ready_for_primary' : 'in_canary';
      }
      return;
    }

    if (!targetVersion) { review.stage = 'no_target'; review.preview = null; return; }

    // No attempt exists yet: compute a live preview of the automatic proposal
    // so the card can show the confidence chart and recommendation before
    // anyone commits to starting the migration.
    review.stage = 'not_started';
    const s = await fetchFor(b, targetVersion, 8);
    review.preview = (s.status === 200 && !s.unreadable) ? proposeVersion(b, p.mapping, s) : null;
    review.canFallback = typeof connectorFor(b).operatorMapping === 'function';
  }

  // What would a migration look like if a person's picks were applied? Read-only:
  // the UI calls this as choices are made so it can show what is still open.
  async function previewMigration(x, ver, choices = {}) {
    const b = asBinding(x);
    const s = await fetchFor(b, ver, 8);
    if (s.status !== 200 || s.unreadable) return null;
    return proposeVersion(b, primary(b).mapping, s, choices.fields || {}, choices.values || {});
  }

  async function createCandidate(b, ver, mapping, note, label) {
    const a = await makeAdapter(b, ver, mapping, 'tested', note);
    b.adapters.push(a); b.proposalFailed = null;
    audit(b, 'PASS', `${versionLabel(b, a)} candidate ready for review (${label})`, 'batch', { actor: 'operator', action: 'ADAPTER_STOOD_UP' });
    emit('adapter.created', { binding: b, adapter: a });
    await runPipeline(b, a);
    await assessMigrationReview(b);
    return a;
  }

  // One action for the non-expert path: use the automatic mapping if every
  // field matched confidently; otherwise use a mapping the upstream's
  // connector can supply (an operator-authored one); otherwise report
  // exactly which fields need a person to choose (`choices` supplies them).
  async function beginMigrationInner(x, ver, o = {}) {
    const b = asBinding(x);
    const p = primary(b);
    const s = await fetchFor(b, ver, 8);
    if (s.status !== 200 || s.unreadable) {
      audit(b, 'BREAKING', `cannot start migration to ${ver}: HTTP ${s.status}`, 'batch', { actor: 'operator', action: 'ADAPTER_STANDUP_FAILED' });
      return { ok: false, reason: `upstream returned HTTP ${s.status}` };
    }
    const forced = (o.choices && o.choices.fields) || {}, forcedValues = (o.choices && o.choices.values) || {};
    const preview = proposeVersion(b, p.mapping, s, forced, forcedValues);
    if (preview.ok) {
      await createCandidate(b, ver, preview.mapping, 'proposed automatically', `${preview.notes.length} mapping decisions`);
      return { ok: true };
    }
    const conn = connectorFor(b);
    if (!Object.keys(forced).length && !Object.keys(forcedValues).length && typeof conn.operatorMapping === 'function') {
      const m = await conn.operatorMapping(b, ver);
      if (m) { await createCandidate(b, ver, clone(m), 'operator authored', 'operator-authored mapping'); return { ok: true, fallback: true }; }
    }
    b.proposalFailed = { ver, unresolved: preview.unresolved };
    audit(b, 'BREAKING', `no automatic mapping for ${ver}: ${preview.unresolved.length} unresolved field${preview.unresolved.length === 1 ? '' : 's'}; a person has to confirm the mapping`, 'batch', { actor: 'operator', action: 'ADAPTER_STANDUP_FAILED' });
    await assessMigrationReview(b);
    return { ok: false, reason: 'needs_mapping', unresolved: preview.unresolved };
  }

  /* ----------------------------- lifecycle actions ----------------------------- */

  async function promoteInner(x, adapterId) {
    const b = asBinding(x);
    const a = b && b.adapters.find(q => q.id === (typeof adapterId === 'string' ? adapterId : adapterId.id));
    if (!a) return { ok: false };
    if (a.state === 'tested') {
      if (!a.lastRun || a.lastRun.readiness !== 'PASS') return { ok: false };
      a.state = 'canary'; a.canaryPass = 0;
      audit(b, 'PASS', `${versionLabel(b, a)} promoted to canary`, 'batch', { actor: 'operator', action: 'ADAPTER_PROMOTED' });
    } else if (a.state === 'canary') {
      if (!a.lastRun || a.lastRun.readiness !== 'PASS' || a.canaryPass < 1) return { ok: false };
      const old = primary(b);
      if (old) { old.state = 'deprecated'; audit(b, 'REVIEW_REQUIRED', `${versionLabel(b, old)} moved from current to previous`, 'batch', { actor: 'operator', action: 'ADAPTER_DEPRECATED' }); }
      a.state = 'primary';
      audit(b, 'PASS', `${versionLabel(b, a)} promoted to current version`, 'batch', { actor: 'operator', action: 'ADAPTER_PROMOTED' });
    } else return { ok: false };
    await assessMigrationReview(b);
    return { ok: true };
  }

  async function retireInner(x, adapterId) {
    const b = asBinding(x);
    const a = b && b.adapters.find(q => q.id === (typeof adapterId === 'string' ? adapterId : adapterId.id));
    if (!a || a.state !== 'deprecated') return { ok: false };
    a.state = 'retired';
    audit(b, 'SYSTEM', `${versionLabel(b, a)} retired`, 'batch', { actor: 'operator', action: 'ADAPTER_RETIRED' });
    return { ok: true };
  }

  // Approval gate: every reviewer choice made, sandbox fully valid, zero
  // shadow differences — re-checked against the upstream right now, not
  // trusted from when the review opened.
  async function approveInner(x) {
    const rv = asReview(x);
    if (!rv || rv.status !== 'open' || rv.kind === 'migration' || rv.choices.some(c => !c.selected)) return { ok: false };
    const b = binding(rv.bindingId), a = b.adapters.find(q => q.id === rv.adapterId);
    const cand = withChoices(rv);
    const ev = await evalCandidate(b, a, cand);
    if (ev.sandbox.valid !== ev.sandbox.total || ev.shadow.diffs.length) { rv.eval = ev; return { ok: false, reason: 'gate' }; }
    cand.version = a.mapping.version + 1; a.mapping = cand;
    await rebaseline(b, a); rv.status = 'approved'; state.metrics.approved++;
    audit(b, 'PASS', `review ${rv.id} approved; ${versionLabel(b, a)} mapping updated to v${cand.version}`, 'batch', { actor: 'reviewer', action: 'REVIEW_APPROVED' });
    await runPipeline(b, a);
    return { ok: true };
  }

  async function rejectInner(x) {
    const rv = asReview(x);
    if (!rv || rv.status !== 'open') return { ok: false };
    rv.status = 'rejected';
    audit(binding(rv.bindingId), 'BREAKING', `review ${rv.id} rejected`, 'batch', { actor: 'reviewer', action: 'REVIEW_REJECTED' });
    return { ok: true };
  }

  // Records a reviewer's pick for one enum choice and re-runs the sandbox
  // with it, so the numbers they see reflect what approving would do.
  async function setReviewChoiceInner(x, index, value) {
    const rv = asReview(x);
    if (!rv || rv.status !== 'open' || !rv.choices[index]) return { ok: false };
    rv.choices[index].selected = value || null;
    const b = binding(rv.bindingId), a = b.adapters.find(q => q.id === rv.adapterId);
    rv.eval = await evalCandidate(b, a, withChoices(rv));
    return { ok: true };
  }

  async function deprecateContractInner(x) {
    const b = asBinding(x), c = b && b.contract;
    if (!c || c.state !== 'ACTIVE') return { ok: false };
    c.state = 'DEPRECATED'; c.sunsetAt = clock.now() + 5 * DAY_MS;
    audit(null, 'REVIEW_REQUIRED', `contract ${b.tool}@${c.version} deprecated; sunset on ${clock.format(c.sunsetAt)}`, 'batch', { actor: 'operator', action: 'CONTRACT_DEPRECATED' });
    return { ok: true };
  }

  /* --------------------------------- registry --------------------------------- */

  // Registers an API. `def`:
  //   { id, displayName?, description?, inputs?, kind?, system?, iface?, connector: '<registered name>',
  //     source?: <opaque, handed to the connector>, contract: { version?, fields: [...] },
  //     version?: '<which upstream version to start on>', mapping?: <initial mapping> }
  // With no `mapping`, one is asked of the connector (operatorMapping) and,
  // failing that, inferred from a live sample — and only accepted if every
  // field matched with confidence. Nothing is registered on a guess.
  async function addBindingInner(def, o = {}) {
    const id = String((def && def.id) || '').trim();
    if (!id) return { ok: false, reason: 'Give the API a name.' };
    if (!ID_RE.test(id)) return { ok: false, reason: 'Use letters, numbers, dots, or underscores, starting with a letter.' };
    if (binding(id)) return { ok: false, reason: `An API called "${id}" already exists.` };
    if (!def.contract || !Array.isArray(def.contract.fields) || !def.contract.fields.length) return { ok: false, reason: 'A binding needs a contract with at least one field.' };
    if (!connectors.has(def.connector)) return { ok: false, reason: `No connector is registered under "${def.connector}".` };

    const contract = clone(def.contract);
    contract.version = contract.version || '1.0.0'; contract.state = contract.state || 'ACTIVE'; contract.sunsetAt = contract.sunsetAt == null ? null : contract.sunsetAt;
    const b = {
      id, tool: def.tool || id, displayName: def.displayName || id, kind: def.kind || 'API', system: def.system || 'custom', iface: def.iface || '',
      description: def.description || '', inputs: def.inputs || null,
      connector: def.connector, source: def.source || {}, contract, canon: contract.fields.map(f => f.name),
      adapters: [], proposalFailed: null, knownVersions: []
    };
    const conn = connectorFor(b);
    let versions = [];
    try { if (typeof conn.listVersions === 'function') versions = await conn.listVersions(b); } catch { /* fall through to the default */ }
    if (!Array.isArray(versions)) versions = []; // a connector may legitimately have nothing to say
    const ver = def.version || versions[0] || 'v1';

    let mapping = def.mapping ? clone(def.mapping) : null;
    if (!mapping && typeof conn.operatorMapping === 'function') { const m = await conn.operatorMapping(b, ver); if (m) mapping = clone(m); }
    if (!mapping) {
      const s = await fetchFor(b, ver, 8);
      if (s.status !== 200 || s.unreadable) return { ok: false, reason: `Could not read the upstream to infer a mapping (HTTP ${s.status}${s.error ? ': ' + s.error : ''}).` };
      const P = inferMapping(b, s);
      if (!P.ok) return { ok: false, reason: `Could not infer a mapping with confidence for: ${P.unresolved.join('; ')}. Supply def.mapping.`, unresolved: P.unresolved, fieldScores: P.fieldScores };
      mapping = P.mapping;
    }

    const a = await makeAdapter(b, ver, mapping, 'primary', 'initial');
    if (!a.baseline) return { ok: false, reason: 'Could not capture a baseline from the upstream (it was unavailable or unreadable).' };
    b.adapters.push(a);
    b.knownVersions = versions.length ? versions : [ver];
    state.bindings.push(b);
    if (!o.quiet) audit(b, 'SYSTEM', `API "${b.displayName}" (${id}, ${b.kind}) added`, 'api', { actor: 'operator', action: 'API_ADDED' });
    emit('binding.added', { binding: b });
    return { ok: true, binding: b };
  }

  async function removeBindingInner(id) {
    const idx = state.bindings.findIndex(b => b.id === id);
    if (idx === -1) return { ok: false, reason: 'That API no longer exists.' };
    const b = state.bindings[idx];
    state.bindings.splice(idx, 1);
    state.reviews = state.reviews.filter(r => r.bindingId !== id);
    audit(null, 'SYSTEM', `API "${b.displayName || id}" (${id}) removed`, 'api', { actor: 'operator', action: 'API_REMOVED' });
    emit('binding.removed', { id, binding: b });
    return { ok: true };
  }

  /* ------------------------------ snapshot / reset ------------------------------ */

  // The whole controller state is plain JSON (mappings, baselines, reviews,
  // audit trail), so persistence is a matter of storing this string.
  const snapshot = () => JSON.stringify({ format: 1, state });
  function restoreInner(json) {
    const d = typeof json === 'string' ? JSON.parse(json) : json;
    if (!d || d.format !== 1 || !d.state) throw new Error('unrecognised snapshot');
    Object.assign(state, fresh(), d.state);
    emit('restored', {});
  }
  function resetInner() { Object.assign(state, fresh()); emit('reset', {}); }

  if (opts.connectors) Object.entries(opts.connectors).forEach(([n, c]) => registerConnector(n, c));

  return {
    state, clock, options, on, registerConnector,
    hooks: { afterTranslate: fn => { hooks.afterTranslate.push(fn); } },
    audit, health, binding, primary, untargeted,
    addBinding: (def, o) => enqueue(() => addBindingInner(def, o)),
    removeBinding: id => enqueue(() => removeBindingInner(id)),
    runBatch: () => enqueue(runBatchInner),
    runPipeline: (x, a) => enqueue(() => { const b = asBinding(x); return runPipeline(b, b.adapters.find(q => q.id === (typeof a === 'string' ? a : a.id))); }),
    liveCall, previewMigration,
    beginMigration: (x, ver, o) => enqueue(() => beginMigrationInner(x, ver, o)),
    assessMigrationReview: x => enqueue(() => assessMigrationReview(x)),
    promote: (x, a) => enqueue(() => promoteInner(x, a)),
    retire: (x, a) => enqueue(() => retireInner(x, a)),
    approve: x => enqueue(() => approveInner(x)),
    reject: x => enqueue(() => rejectInner(x)),
    setReviewChoice: (x, i, v) => enqueue(() => setReviewChoiceInner(x, i, v)),
    deprecateContract: x => enqueue(() => deprecateContractInner(x)),
    snapshot,
    // Queued like every other state change, so a reset can never land in
    // the middle of an operation that is still in flight.
    restore: json => enqueue(() => restoreInner(json)),
    reset: () => enqueue(() => resetInner())
  };
}
