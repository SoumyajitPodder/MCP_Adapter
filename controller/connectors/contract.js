// The connector contract: the ONLY thing the controller knows about an
// upstream. A connector is a plain object:
//
//   async fetch(binding, version, { limit, hints, args }) -> FetchResult (required)
//   async listVersions(binding)                      -> string[]         (optional)
//   async operatorMapping(binding, version)          -> mapping | null   (optional)
//
// FetchResult:
//   status      200 ok · 404 not found · 410 gone/retired · anything else = unavailable
//   records     array of FLAT objects (nested JSON flattened to dot paths)
//   columns     ordered column names for tabular sources (CSV, SQL), else null
//   headers     free-form metadata
//   retirement  { at: <ms timestamp, same clock as the controller> } | null
//   format      parse settings actually used (e.g. { delimiter }) — echoed back
//               to the connector as `hints` on later fetches so a format change
//               is detected instead of silently followed
//   unreadable  true if the body could not be parsed at all
//   error       human-readable reason when status is not 200
//
// `args` is present when an agent asked for ONE specific record. A connector
// must use it safely (encode it, bind it as a parameter — never splice it
// into a URL or a query) and answer 404 if nothing matches, 400 if the
// arguments are unusable, or 400 if it has no way to look anything up.
// With no `args`, `fetch` returns up to `limit` sample records for monitoring.
//
// `listVersions` is how the controller learns a new version exists.
// `operatorMapping` is where a human- or config-supplied mapping can come
// from when automatic matching isn't confident enough; a connector that
// can't provide one simply omits it, and the controller asks a person.

export function validateConnector(impl, name = 'connector') {
  if (!impl || typeof impl.fetch !== 'function') {
    throw new TypeError(`${name}: a connector must have an async fetch(binding, version, { limit, hints }) method`);
  }
  for (const opt of ['listVersions', 'operatorMapping']) {
    if (impl[opt] !== undefined && typeof impl[opt] !== 'function') {
      throw new TypeError(`${name}: ${opt} must be a function when provided`);
    }
  }
  return impl;
}
