// Adapter construction and baseline capture. An adapter binds one
// registered mapping to one upstream version of one binding, and moves
// through tested -> canary -> primary -> deprecated -> retired (see
// core/lifecycle.js for the transitions themselves).

import { state, CONTRACTS } from '../state.js';
import { fetchUpstream } from '../simulation/upstreams.js';
import { analyzeRaw } from './detector.js';
import { translate } from './validator.js';
import { pathsOf, stripPrefix } from './utils.js';

// Builds a straightforward 1:1 mapping from an upstream shape to the
// tool's canonical fields. Used at bootstrap, and again whenever an
// operator authors a mapping by hand instead of accepting a proposed one.
export function deriveMapping(b, shape) {
  const c = CONTRACTS[b.tool];
  return {
    version: 1, unwrap: shape.envelope, sourceTz: 'UTC', coerce: false,
    fields: b.canon.map(name => {
      const f = shape.fields.find(x => x.canon === name);
      const cf = c.fields.find(x => x.name === name);
      const m = { target: name, src: f.src };
      if (cf.type === 'string') m.transform = 'identity';
      if (cf.type === 'enum') { m.transform = 'enum'; m.values = Object.fromEntries(Object.entries(shape.enumMap).map(([k, v]) => [v, k])); }
      if (cf.type === 'datetime') { m.transform = 'datetime'; m.format = shape.dateFormat; }
      return m;
    })
  };
}

// Captures a fresh baseline (field set, types, columns) and a fresh
// last-known-good cache for an adapter. Called on creation, and again
// whenever a mapping is approved, so the next detect() has something to
// compare against.
export function rebaseline(b, a) {
  const s = fetchUpstream(b, a.upstreamVersion, 6);
  if (s.status !== 200) return;
  const delim = a.baseline ? a.baseline.delimiter : (b.upstream.versions[a.upstreamVersion] || {}).delimiter || ',';
  const raw = analyzeRaw(b, s, delim);
  const recs = a.mapping.unwrap ? raw.records.map(x => stripPrefix(x, a.mapping.unwrap + '.')) : raw.records;
  a.baseline = { paths: pathsOf(recs), columns: raw.columns || null, delimiter: delim };
  const t = translate(a.mapping, recs, {});
  a.lkg = {};
  t.out.forEach(o => a.lkg[o[b.canon[0]]] = o);
}

export function makeAdapter(b, ver, mapping, state0, note) {
  const a = {
    id: b.id + '/a' + (b.adapters.length + 1),
    n: b.adapters.length + 1,
    state: state0,
    upstreamVersion: ver,
    mapping,
    note: note || '',
    createdDay: state.day,
    lastRun: null,
    canaryPass: 0,
    seen: {},
    baseline: null,
    lkg: {}
  };
  rebaseline(b, a);
  return a;
}

// Looks up a field's allowed canonical enum values, used when proposing an
// enum mapping for a value the classifier hasn't seen before.
export function contractEnum(b, target) {
  return CONTRACTS[b.tool].fields.find(f => f.name === target).values;
}
