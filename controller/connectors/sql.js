// SQL connector. Driver-agnostic: you pass `query(sql) -> Promise<row[]>`
// (pg, mysql2, better-sqlite3, node:sqlite, … wrapped to return plain row objects).
//
//   binding.source = { versions: { v1: {
//     sql: 'SELECT * FROM orders ORDER BY id LIMIT {limit}',                 // sample rows, for monitoring
//     lookup: { sql: 'SELECT * FROM orders WHERE id = :order_id', params: ['order_id'] }   // one row, for an agent's call
//   } } }
//   (single-version shorthand: { sql, version: 'v1' })
//
// Use SELECT * (or a view): a renamed or dropped column then shows up as
// drift in the rows. An explicit column list that references a column
// which no longer exists fails inside the database instead — that is
// reported as status 410 ("gone"), not as a schema diff.
// {limit} is replaced with the integer sample size (never user text).
// Arguments are NEVER spliced into SQL: each declared :name becomes a bound
// parameter (? by default, or $1,$2… with { placeholder: 'dollar' } for pg),
// and `query(sql, values, binding)` receives the values separately.

import { plainValue, versionsOf } from './helpers.js';

const GONE = /no such (table|column)|does not exist|doesn't exist|unknown (table|column)|undefined (table|column)|invalid (object|column) name/i;

// :name -> ? (or $n), collecting the values in order. Only names declared in
// `params` are recognised, so a colon inside a string literal or a ::cast is left alone.
function bindParams(sql, names, args, placeholder) {
  const values = [];
  if (!names.length) return { sql, values };
  if (names.some(n => !/^[A-Za-z_]\w*$/.test(n))) throw new Error('lookup.params must be plain identifiers');
  const re = new RegExp('(?<![:\\w]):(' + names.join('|') + ')\\b', 'g');
  const out = sql.replace(re, (_, n) => { values.push(args[n]); return placeholder === 'dollar' ? '$' + values.length : '?'; });
  return { sql: out, values };
}

export function createSqlConnector({ query, classifyError, placeholder = 'question' } = {}) {
  if (typeof query !== 'function') throw new Error('createSqlConnector needs query(sql) -> Promise<row[]>');
  const classify = classifyError || (e => (GONE.test(String((e && e.message) || e)) ? 410 : 503));
  return {
    async fetch(binding, version, { limit = 5, args } = {}) {
      const spec = versionsOf(binding.source || {})[version];
      const single = !!(args && Object.keys(args).length);
      let sql, values = [];
      const n = Math.max(1, Math.floor(single ? 10 : Number(limit)) || 1);
      if (single) {
        const lk = spec && spec.lookup;
        if (!lk || !lk.sql) return { status: 400, records: [], error: 'this source does not accept arguments' };
        const missing = (lk.params || []).find(p => args[p] === undefined || args[p] === null);
        if (missing) return { status: 400, records: [], error: `missing argument "${missing}"` };
        ({ sql, values } = bindParams(lk.sql.replace(/\{limit\}/g, String(n)), lk.params || [], args, placeholder));
      } else {
        if (!spec || !spec.sql) return { status: 404, records: [], error: `no query configured for ${version}` };
        sql = spec.sql.replace(/\{limit\}/g, String(n));
      }
      let rows;
      try { rows = await query(sql, values, binding); }
      catch (e) { return { status: classify(e), records: [], error: String((e && e.message) || e) }; }
      if (single && !(rows || []).length) return { status: 404, records: [], error: 'no matching row' };
      const records = (rows || []).slice(0, n).map(r => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, plainValue(v)])));
      return { status: 200, records, columns: records[0] ? Object.keys(records[0]) : [] };
    },
    async listVersions(binding) { return Object.keys(versionsOf(binding.source || {})); }
  };
}
