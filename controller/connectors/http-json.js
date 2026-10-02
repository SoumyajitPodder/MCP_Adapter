// HTTP + JSON connector. Works with any API that returns JSON.
//
//   binding.source = {
//     headers: { Authorization: '...' },                    // optional
//     versions: {
//       v2: { probes: ['https://api/orders/1', '.../2'],    // sample records, for monitoring
//             lookup: { url: 'https://api/orders/{order_id}' } },  // one record, for an agent's call
//       v3: { probes: [...], recordsPath: 'data.items' }    // or a list endpoint: records live at a path
//     }
//   }
//   (single-version shorthand: { probes: [...], version: 'v1' })
//
// A `Sunset` response header (RFC 8594) becomes `retirement`. HTTP 410/404
// pass through as status; network errors and timeouts become status 599.

import { toRecords, pick, versionsOf } from './helpers.js';

// Fills {name} placeholders from args, URL-encoding every value, so an
// argument can never change which host or path is requested.
function fillTemplate(tpl, args) {
  let missing = null;
  const value = tpl.replace(/\{([A-Za-z_]\w*)\}/g, (_, n) => {
    if (args[n] === undefined || args[n] === null) { missing = missing || n; return ''; }
    return encodeURIComponent(String(args[n]));
  });
  return { value, missing };
}

export function createHttpJsonConnector({ fetch: fetchFn, headers = {}, timeoutMs = 10000 } = {}) {
  const doFetch = fetchFn || globalThis.fetch;
  if (typeof doFetch !== 'function') throw new Error('createHttpJsonConnector needs a fetch implementation (Node 18+, a browser, or pass { fetch })');

  async function get(url, extra) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try { return await doFetch(url, { headers: { Accept: 'application/json', ...headers, ...extra }, signal: ctl.signal }); }
    finally { clearTimeout(timer); }
  }

  return {
    async fetch(binding, version, { limit = 5, args } = {}) {
      const src = binding.source || {};
      const spec = versionsOf(src)[version];
      if (!spec) return { status: 404, records: [], error: `no source configured for ${version}` };
      const single = !!(args && Object.keys(args).length);
      let urls, recordsPath = spec.recordsPath;
      if (single) {
        const lk = spec.lookup;
        if (!lk || !lk.url) return { status: 400, records: [], error: 'this source does not accept arguments' };
        const f = fillTemplate(lk.url, args);
        if (f.missing) return { status: 400, records: [], error: `missing argument "${f.missing}"` };
        urls = [f.value]; recordsPath = lk.recordsPath || recordsPath;
      } else urls = [].concat(spec.probes || spec.url || []).slice(0, limit);
      const records = []; let status = 200, error = null, retirement = null;
      for (const url of urls) {
        let res;
        try { res = await get(url, src.headers); }
        catch (e) { return { status: 599, records: [], error: e.name === 'AbortError' ? `timed out after ${timeoutMs}ms` : String(e.message || e) }; }
        if (!retirement) {
          const sunset = res.headers && res.headers.get && res.headers.get('sunset');
          const at = sunset ? Date.parse(sunset) : NaN;
          if (!Number.isNaN(at)) retirement = { at };
        }
        if (res.status === 410) { status = 410; error = `HTTP 410 from ${url}`; break; }
        if (!res.ok) { if (status === 200) { status = res.status; error = `HTTP ${res.status} from ${url}`; } continue; }
        let body;
        try { body = await res.json(); } catch { return { status: 200, records: [], unreadable: true, retirement }; }
        records.push(...toRecords(pick(body, recordsPath)));
      }
      if (single && status === 200 && !records.length) return { status: 404, records: [], retirement, error: 'no matching record' };
      return { status, records: status === 200 ? records.slice(0, single ? 10 : limit) : [], retirement, error };
    },
    async listVersions(binding) { return Object.keys(versionsOf(binding.source || {})); }
  };
}
