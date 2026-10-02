// Bearer-token authentication with two roles:
//   agent  — read tool definitions, call tools (what an MCP environment needs)
//   admin  — everything, including approving reviews and running migrations
// Separation of duties is the point: an agent can never approve its own fix.
//
// Tokens are compared by SHA-256 digest in constant time, every candidate is
// checked (no early exit), and secrets come from the environment, not config.

import crypto from 'node:crypto';

const sha = s => crypto.createHash('sha256').update(String(s)).digest();
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);
export const isLoopback = host => LOOPBACK.has(String(host).toLowerCase());

export function createAuth({ tokens = [], allowAnonymous = false, host = '127.0.0.1' } = {}) {
  if (!tokens.length) {
    if (!allowAnonymous) throw new Error('refusing to start with no auth tokens: configure tokens, or set allowAnonymous: true for local development');
    if (!isLoopback(host)) throw new Error(`refusing anonymous access on non-loopback host "${host}"`);
  }
  const entries = tokens.map(t => {
    if (!t || !t.name || !['agent', 'admin'].includes(t.role)) throw new Error('each token needs a name and a role of "agent" or "admin"');
    if (typeof t.token !== 'string' || t.token.length < 16) throw new Error(`token "${t.name}" is missing or shorter than 16 characters`);
    return { name: t.name, role: t.role, digest: sha(t.token) };
  });
  const anonymous = { name: 'anonymous', role: 'admin' };

  return {
    anonymous: !entries.length,
    authenticate(req) {
      if (!entries.length) return anonymous;
      const m = /^Bearer\s+(\S+)$/i.exec(req.headers.authorization || '');
      if (!m) return null;
      const d = sha(m[1]);
      let found = null;
      for (const e of entries) if (crypto.timingSafeEqual(d, e.digest) && !found) found = e;
      return found ? { name: found.name, role: found.role } : null;
    },
    can: (identity, needed) => identity.role === 'admin' || identity.role === needed
  };
}
