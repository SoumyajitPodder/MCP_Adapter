// Builds connectors from declarative config, so the server can be configured
// without writing code. Secrets are never stored in config: any header value
// of the form "env:NAME" is read from the environment when the connector is
// built, and the resolved value lives only inside the connector.

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHttpJsonConnector, createDelimitedFileConnector, createSqlConnector, createRemoteConnector } from '../../controller/index.js';

export function resolveEnv(v) {
  if (typeof v !== 'string' || !v.startsWith('env:')) return v;
  const name = v.slice(4), val = process.env[name];
  if (val === undefined || val === '') throw new Error(`environment variable ${name} is not set`);
  return val;
}
const resolveHeaders = h => Object.fromEntries(Object.entries(h || {}).map(([k, v]) => [k, resolveEnv(v)]));

// Reads files only from inside `root`: a ".." or an absolute path or a
// symlink that points outside it is refused, not followed.
function fsReader(root) {
  const realRoot = fs.realpathSync(path.resolve(root));
  return async p => {
    const real = await fs.promises.realpath(path.resolve(realRoot, p)); // ENOENT propagates (reported as 404)
    if (real !== realRoot && !real.startsWith(realRoot + path.sep)) throw Object.assign(new Error('path escapes the configured root'), { code: 'EACCES' });
    return fs.promises.readFile(real, 'utf8');
  };
}

export const CONNECTOR_TYPES = {
  // GET JSON over HTTP(S): probes for monitoring, a lookup URL template for calls.
  'http-json': cfg => createHttpJsonConnector({ headers: resolveHeaders(cfg.headers), timeoutMs: cfg.timeoutMs }),
  // A service YOU run, in any language, that answers /fetch (and optionally /versions, /mapping).
  'remote': cfg => createRemoteConnector({ url: cfg.url, headers: resolveHeaders(cfg.headers), timeoutMs: cfg.timeoutMs, capabilities: cfg.capabilities }),
  // CSV/TSV/pipe files under a root directory.
  'delimited-file': cfg => createDelimitedFileConnector({ readText: fsReader(cfg.root) }),
  // A SQLite file, opened read-only where the runtime supports it (Node 22.5+).
  'sqlite': async (cfg, { baseDir }) => {
    const { DatabaseSync } = await import('node:sqlite');
    const file = path.resolve(baseDir, cfg.path);
    let db; try { db = new DatabaseSync(file, { readOnly: true }); } catch { db = new DatabaseSync(file); }
    return createSqlConnector({ query: async (sql, values = []) => db.prepare(sql).all(...values) });
  },
  // Anything else (Postgres, MySQL, Kafka, a mainframe...): a module whose default export is
  // `async (options) => connector`. Config is trusted input, like the code it points at.
  'module': async (cfg, { baseDir }) => {
    const mod = await import(pathToFileURL(path.resolve(baseDir, cfg.path)).href);
    const factory = mod.default || mod.createConnector;
    if (typeof factory !== 'function') throw new Error(`${cfg.path} must export a default function (options) => connector`);
    return factory(cfg.options || {});
  }
};

export async function buildConnectors(spec = {}, { baseDir = process.cwd() } = {}) {
  const out = {};
  for (const [name, cfg] of Object.entries(spec)) {
    const make = CONNECTOR_TYPES[cfg && cfg.type];
    if (!make) throw new Error(`connector "${name}": unknown type "${cfg && cfg.type}" (known: ${Object.keys(CONNECTOR_TYPES).join(', ')})`);
    try { out[name] = await make(cfg, { baseDir }); }
    catch (e) { throw new Error(`connector "${name}": ${e.message}`); }
  }
  return out;
}
