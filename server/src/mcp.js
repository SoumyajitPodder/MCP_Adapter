// A minimal MCP server over Streamable HTTP (JSON-RPC 2.0 on POST /mcp,
// stateless, JSON responses). It exposes exactly the read-only tools —
// never review/migration controls — so an agent can use the controller but
// can't approve its own fix.

const SUPPORTED = ['2025-06-18', '2025-03-26', '2024-11-05'];
export const rpcError = (id, code, message, data) => ({ jsonrpc: '2.0', id: id ?? null, error: { code, message, ...(data ? { data } : {}) } });
const ok = (id, result) => ({ jsonrpc: '2.0', id, result });

// Returns a JSON-RPC response, or null for a notification (HTTP 202, no body).
export async function handleMcpMessage(msg, { listTools, callTool, serverInfo }) {
  if (!msg || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') return rpcError(msg && msg.id, -32600, 'Invalid Request');
  const isNotification = msg.id === undefined || msg.id === null;
  switch (msg.method) {
    case 'initialize': {
      const asked = msg.params && msg.params.protocolVersion;
      return ok(msg.id, {
        protocolVersion: SUPPORTED.includes(asked) ? asked : SUPPORTED[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo,
        instructions: 'Read-only tools backed by the lifecycle controller. Outputs follow a stable contract; if an upstream system changes in a way that cannot be absorbed safely, calls fail closed with an explanatory error rather than returning guesses.'
      });
    }
    case 'ping': return isNotification ? null : ok(msg.id, {});
    case 'tools/list': return ok(msg.id, { tools: listTools() });
    case 'tools/call': {
      const name = msg.params && msg.params.name;
      if (!listTools().some(t => t.name === name)) return rpcError(msg.id, -32602, `Unknown tool: ${name}`);
      const r = await callTool(name, msg.params.arguments);
      const text = JSON.stringify(r.body, null, 2);
      return ok(msg.id, r.status === 200
        ? { content: [{ type: 'text', text }], structuredContent: r.body, isError: false }
        : { content: [{ type: 'text', text }], isError: true });
    }
    default:
      return isNotification ? null : rpcError(msg.id, -32601, `Method not found: ${msg.method}`);
  }
}
