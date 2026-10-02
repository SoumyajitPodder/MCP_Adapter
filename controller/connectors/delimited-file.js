// Delimited text (CSV/TSV/pipe) connector. Storage-agnostic: you pass
// `readText(path, binding)` — fs.readFile, an S3 getObject, an SFTP read,
// whatever returns the file's text.
//
//   binding.source = { versions: { v1: { path: '/data/inventory.csv', delimiter: ',',
//                                         lookup: { column: 'item_id', arg: 'item_id' } } } }   // lookup: one row by exact match
//   (single-version shorthand: { path, version: 'v1' })
//
// The delimiter the baseline was captured with comes back as `hints` and
// is used on every later read — so a feed that switches from "," to "|" is
// reported as an unreadable format instead of being quietly re-parsed.

import { parseDelimited, versionsOf } from './helpers.js';

export function createDelimitedFileConnector({ readText } = {}) {
  if (typeof readText !== 'function') throw new Error('createDelimitedFileConnector needs readText(path, binding) -> Promise<string>');
  return {
    async fetch(binding, version, { limit = 5, hints, args } = {}) {
      const spec = versionsOf(binding.source || {})[version];
      if (!spec || !spec.path) return { status: 404, records: [], error: `no file configured for ${version}` };
      let text;
      try { text = await readText(spec.path, binding); }
      catch (e) {
        const missing = e && (e.code === 'ENOENT' || /not found|no such file/i.test(String(e.message)));
        return { status: missing ? 404 : 503, records: [], error: String((e && e.message) || e) };
      }
      const parsed = parseDelimited(text, (hints && hints.delimiter) || spec.delimiter || ',');
      if (parsed.unreadable) return { status: 200, records: [], unreadable: true, format: parsed.format };
      if (args && Object.keys(args).length) {
        const lk = spec.lookup;
        if (!lk || !lk.column) return { status: 400, records: [], error: 'this source does not accept arguments' };
        const want = args[lk.arg || lk.column];
        if (want === undefined || want === null) return { status: 400, records: [], error: `missing argument "${lk.arg || lk.column}"` };
        const hit = parsed.records.filter(r => String(r[lk.column]) === String(want));
        return hit.length ? { status: 200, records: hit.slice(0, 10), columns: parsed.columns, format: parsed.format } : { status: 404, records: [], error: 'no matching row' };
      }
      return { status: 200, records: parsed.records.slice(0, limit), columns: parsed.columns, format: parsed.format };
    },
    async listVersions(binding) { return Object.keys(versionsOf(binding.source || {})); }
  };
}
