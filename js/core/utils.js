// Small, generic helpers shared across the core modules. This file has no
// dependencies of its own — it does not read application state and does
// not touch the DOM — so everything else is free to import from it
// without creating a circular dependency.

export const clone = o => JSON.parse(JSON.stringify(o));

// A friendly API name like "Billing API" becomes an id like "billing_api" —
// used when adding a new API, so the person naming it never has to think
// about id syntax rules themselves.
export const slugify = s => (s || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'api';
export const pad = n => String(n).padStart(2, '0');
export const norm = s => String(s).toLowerCase().replace(/[^a-z0-9]/g, '');

// Levenshtein edit distance, used by nameSim below.
export function lev(a, b) {
  const m = a.length, n = b.length;
  if (!m) return n;
  if (!n) return m;
  let p = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const c = [i];
    for (let j = 1; j <= n; j++) {
      c[j] = Math.min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    p = c;
  }
  return p[n];
}

// Normalized name similarity, 0..1, used by the classifier's confidence score.
export function nameSim(a, b) {
  a = norm(a); b = norm(b);
  if (!a || !b) return 0;
  return 1 - lev(a, b) / Math.max(a.length, b.length);
}

// Flips a field name between camelCase and snake_case, used by the
// "rename first field" drift injector to simulate a case-style rename.
export const toggleCase = s => s.includes('_')
  ? s.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
  : s.replace(/([A-Z])/g, '_$1').toLowerCase();

// Flattens a nested object into dot-path keys, e.g. {a:{b:1}} -> {'a.b':1}.
export function flatten(o, pre = '', out = {}) {
  for (const [k, v] of Object.entries(o)) {
    const p = pre ? pre + '.' + k : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, p, out);
    else out[p] = v;
  }
  return out;
}

export const tname = v => Array.isArray(v) ? 'array' : v === null ? 'null' : typeof v;

// Strips a leading "prefix." from every key, used to unwrap an envelope.
export function stripPrefix(r, pre) {
  const o = {};
  for (const k in r) o[k.startsWith(pre) ? k.slice(pre.length) : k] = r[k];
  return o;
}

// Field name -> inferred type, taken from the first record that has it.
export function pathsOf(records) {
  const o = {};
  records.forEach(r => { for (const k in r) if (!(k in o)) o[k] = tname(r[k]); });
  return o;
}

export const inferFmt = v => typeof v !== 'string' ? 'OTHER'
  : /^\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}:\d{2}$/.test(v) ? 'US'
  : /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(v) ? 'ISO'
  : 'OTHER';

export const isoZ = d => d.toISOString().replace(/\.\d{3}Z$/, 'Z');

export const fmtDate = (d, f) => f === 'US'
  ? `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}/${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
  : isoZ(d);

// Severity ordering used everywhere a "worst classification wins" comparison
// is made (schema comparison stage, review triggers, drift metrics).
export const SEV = { COMPATIBLE: 0, REVIEW_REQUIRED: 1, BREAKING: 2, UNKNOWN: 3 };

// The adapter currently serving live traffic for a binding, if any.
// Lives here (not in core/adapters.js) so both the adapter module and the
// simulator can use it without importing each other.
export const primary = b => b.adapters.find(a => a.state === 'primary');

const ROLE_WORD = {
  primary: 'current version', tested: 'candidate, not yet live', canary: 'candidate in canary',
  deprecated: 'previous version', retired: 'retired', rolled_back: 'rolled back'
};

// How many adapters this binding has pointed at the same upstream version —
// used to disambiguate a second (or third) attempt at the same version
// from the first, since "v3" alone stops being a unique label the moment a
// migration is retried after a rollback.
function attemptLabel(b, a) {
  const sameVer = b.adapters.filter(x => x.upstreamVersion === a.upstreamVersion);
  if (sameVer.length <= 1) return '';
  const idx = sameVer.findIndex(x => x.id === a.id) + 1;
  return ` (attempt ${idx})`;
}

// A short label for contexts that already group by state (the lifecycle
// rail's lanes, for instance) — just the version and, if needed, which
// attempt this is.
export function versionLabel(b, a) {
  return `${a.upstreamVersion}${attemptLabel(b, a)}`;
}

// A fuller label for contexts that span every state at once (the adapter
// picker tabs on the Pipeline and Mapping views) — the version, which
// attempt if that's ambiguous, and what it currently is to this binding.
export function adapterLabel(b, a) {
  return `${versionLabel(b, a)} — ${ROLE_WORD[a.state] || a.state}`;
}
