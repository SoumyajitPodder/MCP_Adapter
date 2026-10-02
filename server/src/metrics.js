// Prometheus text-format metrics. Counters live here; gauges are read from
// the controller at scrape time so they can never go stale.

const esc = v => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
const lbl = o => { const k = Object.keys(o); return k.length ? '{' + k.map(x => `${x}="${esc(o[x])}"`).join(',') + '}' : ''; };

export function createMetrics(controller, getInflight) {
  const calls = new Map(), dur = new Map(), http = new Map();
  const bump = (m, key, by = 1) => m.set(key, (m.get(key) || 0) + by);
  return {
    observeCall(tool, outcome, seconds) { bump(calls, JSON.stringify({ tool, outcome })); bump(dur, JSON.stringify({ tool }), seconds); bump(dur, JSON.stringify({ tool, n: 1 })); },
    observeHttp(route, status) { bump(http, JSON.stringify({ route, status: String(status) })); },
    render() {
      const L = [], s = controller.state;
      const head = (n, h, t) => L.push(`# HELP ${n} ${h}`, `# TYPE ${n} ${t}`);
      head('lifecycle_tool_calls_total', 'Tool calls by outcome.', 'counter');
      for (const [k, v] of calls) L.push(`lifecycle_tool_calls_total${lbl(JSON.parse(k))} ${v}`);
      head('lifecycle_tool_call_seconds_sum', 'Total time spent in tool calls.', 'counter');
      head('lifecycle_tool_call_seconds_count', 'Number of timed tool calls.', 'counter');
      for (const [k, v] of dur) { const o = JSON.parse(k); if (o.n) L.push(`lifecycle_tool_call_seconds_count${lbl({ tool: o.tool })} ${v}`); else L.push(`lifecycle_tool_call_seconds_sum${lbl(o)} ${v.toFixed(6)}`); }
      head('lifecycle_http_requests_total', 'HTTP requests by route and status.', 'counter');
      for (const [k, v] of http) L.push(`lifecycle_http_requests_total${lbl(JSON.parse(k))} ${v}`);
      head('lifecycle_inflight_calls', 'Tool calls currently in flight.', 'gauge'); L.push(`lifecycle_inflight_calls ${getInflight()}`);
      head('lifecycle_bindings', 'Configured bindings.', 'gauge'); L.push(`lifecycle_bindings ${s.bindings.length}`);
      head('lifecycle_reviews_open', 'Reviews waiting on a person.', 'gauge'); L.push(`lifecycle_reviews_open ${s.reviews.filter(r => r.status === 'open').length}`);
      head('lifecycle_batches_total', 'Batch runs completed.', 'counter'); L.push(`lifecycle_batches_total ${s.batches}`);
      head('lifecycle_drift_absorbed_total', 'Changes absorbed automatically.', 'counter'); L.push(`lifecycle_drift_absorbed_total ${s.metrics.absorbed}`);
      head('lifecycle_drift_blocked_total', 'Breaking changes blocked.', 'counter'); L.push(`lifecycle_drift_blocked_total ${s.metrics.blocked}`);
      head('lifecycle_binding_health', '1 for each binding\'s current health state.', 'gauge');
      for (const b of s.bindings) L.push(`lifecycle_binding_health${lbl({ binding: b.id, status: controller.health(b) })} 1`);
      return L.join('\n') + '\n';
    }
  };
}
