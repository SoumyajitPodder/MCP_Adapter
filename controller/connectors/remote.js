// Remote connector: delegates reading the upstream to a service YOU run, in
// any language. This is how a system the controller can't talk to directly
// (a Python gateway, a mainframe adapter, an MCP server's own client) plugs
// in: expose three tiny endpoints and describe nothing else.
//
//   POST {url}/fetch      { binding: { id, tool, source }, version, limit, hints, args }  -> FetchResult
//   POST {url}/versions   { binding }                                                     -> { versions: [...] }      (optional)
//   POST {url}/mapping    { binding, version }                                            -> { mapping } | { mapping: null }  (optional)
//
// FetchResult is exactly the connector contract's shape (status, records,
// columns, retirement, format, unreadable, error). If your service itself
// answers 5xx the controller sees "unavailable"; it never sees an exception.
// Enable /versions with capabilities.listVersions (default true) and
// /mapping with capabilities.operatorMapping (default false).

export function createRemoteConnector({ url, headers = {}, timeoutMs = 10000, fetch: fetchFn, capabilities = {} } = {}) {
  if (!url) throw new Error('createRemoteConnector needs { url }');
  const doFetch = fetchFn || globalThis.fetch;
  const base = String(url).replace(/\/+$/, '');

  async function post(path, body) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await doFetch(base + path, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json', ...headers }, body: JSON.stringify(body), signal: ctl.signal });
      let json = null;
      try { json = await res.json(); } catch { /* not JSON */ }
      return { res, json };
    } finally { clearTimeout(timer); }
  }
  const describe = b => ({ id: b.id, tool: b.tool, source: b.source });

  const conn = {
    async fetch(binding, version, { limit, hints, args } = {}) {
      let r;
      try { r = await post('/fetch', { binding: describe(binding), version, limit, hints, args }); }
      catch (e) { return { status: 599, records: [], error: e.name === 'AbortError' ? `remote connector timed out after ${timeoutMs}ms` : String(e.message || e) }; }
      if (r.res.status >= 500) return { status: 503, records: [], error: `remote connector answered HTTP ${r.res.status}` };
      if (!r.json || typeof r.json !== 'object') return { status: 502, records: [], error: 'remote connector did not return JSON' };
      return r.json;
    }
  };
  if (capabilities.listVersions !== false) {
    conn.listVersions = async binding => {
      const r = await post('/versions', { binding: describe(binding) });
      if (!r.res.ok || !r.json || !Array.isArray(r.json.versions)) return null;
      return r.json.versions;
    };
  }
  if (capabilities.operatorMapping) {
    conn.operatorMapping = async (binding, version) => {
      const r = await post('/mapping', { binding: describe(binding), version });
      return r.res.ok && r.json && r.json.mapping ? r.json.mapping : null;
    };
  }
  return conn;
}
