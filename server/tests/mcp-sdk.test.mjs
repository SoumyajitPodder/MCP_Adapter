// Interoperability with the OFFICIAL MCP client SDK. My own tests of /mcp only
// prove I read the spec consistently; this proves a real MCP client can use
// the server. Skipped unless the SDK is installed (npm install in server/).
import test from 'node:test';
import assert from 'node:assert/strict';
import { boot, AGENT } from './fixture.mjs';

let sdk = null;
try {
  const c = await import('@modelcontextprotocol/sdk/client/index.js');
  const t = await import('@modelcontextprotocol/sdk/client/streamableHttp.js');
  sdk = { Client: c.Client, Transport: t.StreamableHTTPClientTransport };
} catch { /* SDK not installed */ }
const skip = sdk ? false : 'install @modelcontextprotocol/sdk (npm install in server/) to run this';

async function connect(srv, token = AGENT) {
  const transport = new sdk.Transport(new URL(srv.url + '/mcp'), { requestInit: { headers: { Authorization: `Bearer ${token}` } } });
  const client = new sdk.Client({ name: 'interop-test', version: '1.0.0' });
  await client.connect(transport);
  return { client, transport };
}

test('the official MCP client connects, lists tools, and calls them', { skip }, async t => {
  const { srv } = await boot(t);
  const { client } = await connect(srv); t.after(() => client.close());
  assert.equal(client.getServerVersion().name, 'lifecycle-controller');
  assert.ok(client.getServerCapabilities().tools);

  const { tools } = await client.listTools();
  assert.deepEqual(tools.map(x => x.name), ['order.get']);
  assert.equal(tools[0].annotations.readOnlyHint, true);
  assert.deepEqual(tools[0].inputSchema.required, ['order_id']);

  // The SDK validates structuredContent against the tool's outputSchema, i.e. against the contract.
  const ok = await client.callTool({ name: 'order.get', arguments: { order_id: 'ORD-1003' } });
  assert.notEqual(ok.isError, true);
  assert.deepEqual(ok.structuredContent.data, [{ order_id: 'ORD-1003', status: 'in_progress', quantity: 4, tracking_number: 'TRK3' }]);
  assert.equal(JSON.parse(ok.content[0].text).data[0].order_id, 'ORD-1003');
});

test('errors reach an MCP client as tool errors it can reason about', { skip }, async t => {
  const { srv, up } = await boot(t);
  const { client } = await connect(srv); t.after(() => client.close());
  await client.listTools();
  const nf = await client.callTool({ name: 'order.get', arguments: { order_id: 'ORD-9999' } });
  assert.equal(nf.isError, true); assert.equal(JSON.parse(nf.content[0].text).error.code, 'NOT_FOUND');
  const bad = await client.callTool({ name: 'order.get', arguments: { nope: 1 } });
  assert.equal(bad.isError, true); assert.match(JSON.parse(bad.content[0].text).error.message, /unknown argument|missing required/);
  up.cfg.shape = 'renamed';
  const drift = await client.callTool({ name: 'order.get', arguments: { order_id: 'ORD-1002' } });
  assert.equal(drift.isError, true); assert.equal(JSON.parse(drift.content[0].text).error.code, 'DRIFT_BLOCKED');
  await assert.rejects(client.callTool({ name: 'no.such.tool', arguments: {} }));
});

test('an MCP client with the wrong token, or with an admin-only ask, gets nowhere', { skip }, async t => {
  const { srv } = await boot(t);
  await assert.rejects(connect(srv, 'wrong-token-000000000'));
  const { client } = await connect(srv); t.after(() => client.close());
  const { tools } = await client.listTools();
  assert.ok(tools.every(x => !/approve|migrat|binding|review/i.test(x.name)), 'no control-plane tool is offered to an agent');
});
