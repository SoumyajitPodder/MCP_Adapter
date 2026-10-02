import fs from 'node:fs';
import path from 'node:path';
import { resolveEnv } from './connectors.js';

// Config shape:
//   { server: { host, port, stateFile, batchIntervalSeconds, maxConcurrentCalls, bodyLimitBytes, corsOrigins, allowAnonymous },
//     tokens: [ { name, role: "agent"|"admin", tokenEnv: "ENV_VAR_NAME" } ],
//     connectors: { <name>: { type, ...options } },
//     bindings: [ { id, connector, source, contract, inputs?, mapping?, ... } ] }
// Tokens are read from the environment (tokenEnv), never from the file.
export function normalizeConfig(raw, baseDir = process.cwd()) {
  const server = { host: '127.0.0.1', port: 8787, ...(raw.server || {}) };
  if (server.stateFile) server.stateFile = path.resolve(baseDir, server.stateFile);
  const tokens = (raw.tokens || []).map(t => ({ name: t.name, role: t.role, token: t.tokenEnv ? resolveEnv('env:' + t.tokenEnv) : t.token }));
  return { server, tokens, connectors: raw.connectors || {}, bindings: raw.bindings || [], baseDir };
}

export function loadConfig(file) {
  const abs = path.resolve(file);
  let raw;
  try { raw = JSON.parse(fs.readFileSync(abs, 'utf8')); }
  catch (e) { throw new Error(`cannot read config ${abs}: ${e.message}`); }
  return normalizeConfig(raw, path.dirname(abs));
}
