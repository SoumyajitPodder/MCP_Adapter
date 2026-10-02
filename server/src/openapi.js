// The OpenAPI document is generated from the same route table the server
// dispatches on, so the documentation cannot drift from the behaviour.

const errorSchema = { type: 'object', properties: { error: { type: 'object', properties: { code: { type: 'string' }, message: { type: 'string' }, details: {} }, required: ['code', 'message'] } }, required: ['error'] };
const ref = n => ({ $ref: `#/components/schemas/${n}` });

export function buildOpenApi({ routes, version }) {
  const paths = {};
  for (const r of routes) {
    const item = (paths[r.path] = paths[r.path] || {});
    const params = r.names.map(n => ({ name: n, in: 'path', required: true, schema: { type: 'string' } }));
    (r.meta.query || []).forEach(q => params.push({ name: q.name, in: 'query', required: false, description: q.description, schema: { type: q.type || 'string' } }));
    const responses = {
      [String(r.meta.ok ? r.meta.ok.status : 200)]: { description: (r.meta.ok && r.meta.ok.description) || 'Success', ...(r.meta.ok && r.meta.ok.schema ? { content: { [r.meta.ok.type || 'application/json']: { schema: r.meta.ok.schema } } } : {}) }
    };
    if (r.role !== 'public') { responses['401'] = { description: 'Missing or invalid bearer token', content: { 'application/json': { schema: ref('Error') } } }; responses['403'] = { description: 'The token\'s role cannot do this', content: { 'application/json': { schema: ref('Error') } } }; }
    if (r.meta.body) responses['400'] = { description: 'Invalid request', content: { 'application/json': { schema: ref('Error') } } };
    Object.entries(r.meta.errors || {}).forEach(([s, d]) => { responses[s] = { description: d, content: { 'application/json': { schema: ref('Error') } } }; });
    item[r.method.toLowerCase()] = {
      operationId: r.meta.id, summary: r.meta.summary, tags: [r.meta.tag],
      security: r.role === 'public' ? [] : [{ bearerAuth: [] }], 'x-required-role': r.role,
      ...(params.length ? { parameters: params } : {}),
      ...(r.meta.body ? { requestBody: { required: !!r.meta.bodyRequired, content: { 'application/json': { schema: r.meta.body } } } } : {}),
      responses
    };
  }
  return {
    openapi: '3.0.3',
    info: { title: 'Lifecycle Controller API', version, description: 'Detects upstream drift, absorbs what is safe, routes the rest to a person, and serves agents a stable contract. `agent` tokens may read tool definitions and call tools; `admin` tokens may do everything. The same tools are available over MCP at POST /mcp.' },
    servers: [{ url: '/' }],
    components: {
      securitySchemes: { bearerAuth: { type: 'http', scheme: 'bearer' } },
      schemas: {
        Error: errorSchema,
        CallRequest: { type: 'object', properties: { args: { type: 'object', additionalProperties: true, description: 'Arguments, validated against the tool\'s inputSchema' } } },
        CallResult: { type: 'object', properties: { data: { type: 'array', items: { type: 'object' } }, _meta: { type: 'object', properties: { contract: { type: 'string' }, served_by: { type: 'string' }, absorbed: { type: 'array', items: { type: 'string' } }, warnings: { type: 'array', items: { type: 'string' } } } } }, required: ['data'] },
        ToolDefinition: { type: 'object', properties: { name: { type: 'string' }, title: { type: 'string' }, description: { type: 'string' }, inputSchema: { type: 'object' }, outputSchema: { type: 'object' }, annotations: { type: 'object' } }, required: ['name', 'inputSchema'] },
        BindingDefinition: { type: 'object', required: ['id', 'connector', 'contract'], properties: { id: { type: 'string' }, displayName: { type: 'string' }, description: { type: 'string' }, connector: { type: 'string', description: 'Name of a connector registered on the server' }, source: { type: 'object', description: 'Opaque, handed to the connector. Keep secrets out of it.' }, contract: { type: 'object', description: '{ version?, fields: [{ name, type: string|enum|datetime|date|number|integer|boolean, values?, optional? }] }' }, inputs: { type: 'object', description: '{ properties, required? } — the tool\'s arguments' }, mapping: { type: 'object', description: 'Optional initial mapping; inferred (and only accepted if confident) when omitted' }, version: { type: 'string' } } },
        MigrationChoices: { type: 'object', properties: { fields: { type: 'object', additionalProperties: { type: 'string' } }, values: { type: 'object', additionalProperties: { type: 'string' } } } }
      }
    },
    paths
  };
}
