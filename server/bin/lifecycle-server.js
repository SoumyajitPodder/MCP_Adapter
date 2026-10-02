#!/usr/bin/env node
// lifecycle-server --config server.config.json [--host H] [--port P] [--state FILE] [--allow-anonymous]
import { loadConfig } from '../src/config.js';
import { buildConnectors } from '../src/connectors.js';
import { createLifecycleServer, VERSION } from '../src/server.js';

const argv = process.argv.slice(2);
const flag = n => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : undefined; };
if (argv.includes('--help') || argv.includes('-h')) {
  console.log(`lifecycle-server ${VERSION}\n\n  --config FILE        server config (see server/README.md)\n  --host HOST          default 127.0.0.1\n  --port PORT          default 8787\n  --state FILE         persist controller state here\n  --allow-anonymous    no auth tokens; loopback only (development)\n`);
  process.exit(0);
}
const log = (level, msg) => console.error(JSON.stringify({ t: new Date().toISOString(), level, msg }));

try {
  const cfg = flag('--config') ? loadConfig(flag('--config')) : { server: {}, tokens: [], connectors: {}, bindings: [], baseDir: process.cwd() };
  const s = { ...cfg.server };
  if (flag('--host')) s.host = flag('--host');
  if (flag('--port')) s.port = Number(flag('--port'));
  if (flag('--state')) s.stateFile = flag('--state');
  if (argv.includes('--allow-anonymous')) s.allowAnonymous = true;
  const connectors = await buildConnectors(cfg.connectors, { baseDir: cfg.baseDir });
  const server = await createLifecycleServer({ ...s, tokens: cfg.tokens, connectors, bindings: cfg.bindings, log });
  log('info', `lifecycle-server ${VERSION} listening on ${server.url} (${server.controller.state.bindings.length} bindings, ${cfg.tokens.length ? cfg.tokens.length + ' tokens' : 'ANONYMOUS'})`);
  const stop = async sig => { log('info', `${sig}: shutting down`); await server.close(); process.exit(0); };
  process.on('SIGINT', () => stop('SIGINT')); process.on('SIGTERM', () => stop('SIGTERM'));
} catch (e) {
  console.error(`lifecycle-server: ${e.message}`);
  process.exit(1);
}
