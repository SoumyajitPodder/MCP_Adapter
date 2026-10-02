// Client for the lifecycle controller server (fetch; Node 18+ or a browser).
//   const agent = new LifecycleClient('http://127.0.0.1:8787', { token });
//   const { data } = await agent.call('get_order_status', { order_id: 'ORDER-8001' });
// Tool calls throw LifecycleError (.code, .status, .review) on anything but success.

export class LifecycleError extends Error {
  constructor(status, error = {}) { super(`${error.code || 'HTTP_ERROR'} (HTTP ${status}): ${error.message || ''}`); Object.assign(this, { status, code: error.code || 'HTTP_ERROR', details: error.details, review: error.review }); }
}

export class LifecycleClient {
  constructor(baseUrl, { token, fetch: f } = {}) { this.base = baseUrl.replace(/\/+$/, ''); this.token = token; this.f = f || globalThis.fetch; }
  async raw(method, path, body) {
    const res = await this.f(this.base + path, { method, headers: { accept: 'application/json', ...(body !== undefined ? { 'content-type': 'application/json' } : {}), ...(this.token ? { authorization: `Bearer ${this.token}` } : {}) }, body: body !== undefined ? JSON.stringify(body) : undefined });
    const text = await res.text(); let json = null; try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { status: res.status, json };
  }
  async ok(method, path, body) { const r = await this.raw(method, path, body); if (r.status >= 400) throw new LifecycleError(r.status, r.json && r.json.error); return r.json; }
  tools() { return this.ok('GET', '/v1/tools').then(r => r.tools); }
  async call(tool, args = {}, { raiseOnError = true } = {}) { const r = await this.raw('POST', `/v1/tools/${encodeURIComponent(tool)}/call`, { args }); if (r.status >= 400 && raiseOnError) throw new LifecycleError(r.status, r.json && r.json.error); return r.json; }
  reviews(status = 'open') { return this.ok('GET', `/v1/reviews?status=${encodeURIComponent(status)}`).then(r => r.reviews); }
  approve(id) { return this.ok('POST', `/v1/reviews/${id}/approve`); }
  reject(id) { return this.ok('POST', `/v1/reviews/${id}/reject`); }
  choose(id, index, value) { return this.ok('POST', `/v1/reviews/${id}/choices`, { index, value }); }
  runBatch() { return this.ok('POST', '/v1/batch'); }
  bindings() { return this.ok('GET', '/v1/bindings').then(r => r.bindings); }
  addBinding(def) { return this.ok('POST', '/v1/bindings', def); }
  previewMigration(id, version, choices) { return this.ok('POST', `/v1/bindings/${id}/migrations/preview`, { version, choices }); }
  beginMigration(id, version, choices) { return this.ok('POST', `/v1/bindings/${id}/migrations`, { version, choices }); }
  promote(id, adapter) { return this.ok('POST', `/v1/bindings/${id}/adapters/${adapter}/promote`); }
  audit(limit = 100) { return this.ok('GET', `/v1/audit?limit=${limit}`).then(r => r.entries); }
}
