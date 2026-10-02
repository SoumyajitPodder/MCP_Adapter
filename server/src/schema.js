// Turns the controller's contracts into the schemas an agent environment
// actually reads: MCP-style tool definitions whose outputSchema IS the
// canonical contract (the stable output shape), plus a small, strict
// validator for tool arguments so nothing reaches an upstream unchecked.

export function contractToJsonSchema(contract) {
  const properties = {}, required = [];
  for (const f of contract.fields) {
    let s;
    switch (f.type) {
      case 'enum': s = { type: 'string', enum: f.values.slice() }; break;
      case 'datetime': s = { type: 'string', format: 'date-time' }; break;
      case 'date': s = { type: 'string', format: 'date' }; break;
      case 'number': s = { type: 'number' }; break;
      case 'integer': s = { type: 'integer' }; break;
      case 'boolean': s = { type: 'boolean' }; break;
      default: s = { type: 'string', minLength: 1 };
    }
    if (f.optional) { s = { ...s, type: [s.type, 'null'] }; if (s.enum) s.enum = [...s.enum, null]; }
    if (f.description) s.description = f.description;
    properties[f.name] = s;
    required.push(f.name); // optional fields are still always present in the output, as null
  }
  return { type: 'object', properties, required, additionalProperties: false };
}

export function inputSchemaFor(b) {
  const inp = b.inputs;
  if (!inp || !inp.properties) return { type: 'object', properties: {}, additionalProperties: false };
  return { type: 'object', properties: inp.properties, required: inp.required || [], additionalProperties: false };
}

export function toolDefinition(b) {
  return {
    name: b.tool,
    title: b.displayName || b.tool,
    description: b.description || `Read ${b.displayName || b.tool}. Output follows the stable contract ${b.tool}@${b.contract.version}; changes in the upstream system are absorbed or blocked, never passed through.`,
    inputSchema: inputSchemaFor(b),
    outputSchema: {
      type: 'object',
      properties: { data: { type: 'array', items: contractToJsonSchema(b.contract) }, _meta: { type: 'object' } },
      required: ['data']
    },
    annotations: { title: b.displayName || b.tool, readOnlyHint: true, idempotentHint: true, openWorldHint: true },
    _meta: { 'lifecycle/contract': `${b.tool}@${b.contract.version}` }
  };
}

const MAX_STRING = 256;

// A deliberately small JSON-Schema subset: flat objects of string / number /
// integer / boolean with required, enum, pattern, min/max length and range.
// Unknown arguments are errors, and strings are length-capped by default.
export function validateArgs(inputs, args) {
  const a = args === undefined || args === null ? {} : args;
  if (typeof a !== 'object' || Array.isArray(a)) return { ok: false, errors: ['arguments must be an object'] };
  const props = (inputs && inputs.properties) || {};
  const errors = [];
  for (const k of Object.keys(a)) if (!(k in props)) errors.push(`unknown argument "${k}"`);
  for (const k of (inputs && inputs.required) || []) if (a[k] === undefined || a[k] === null) errors.push(`missing required argument "${k}"`);
  for (const [k, spec] of Object.entries(props)) {
    const v = a[k];
    if (v === undefined || v === null) continue;
    const t = spec.type;
    if (t === 'string') {
      if (typeof v !== 'string') { errors.push(`"${k}" must be a string`); continue; }
      if (v.length > (spec.maxLength ?? MAX_STRING)) errors.push(`"${k}" is longer than ${spec.maxLength ?? MAX_STRING} characters`);
      if (spec.minLength !== undefined && v.length < spec.minLength) errors.push(`"${k}" is shorter than ${spec.minLength} characters`);
      if (spec.pattern) { try { if (!new RegExp(spec.pattern).test(v)) errors.push(`"${k}" does not match the required pattern`); } catch { errors.push(`"${k}" has an invalid pattern in its schema`); } }
    } else if (t === 'integer' || t === 'number') {
      if (typeof v !== 'number' || !Number.isFinite(v) || (t === 'integer' && !Number.isInteger(v))) { errors.push(`"${k}" must be ${t === 'integer' ? 'an integer' : 'a number'}`); continue; }
      if (spec.minimum !== undefined && v < spec.minimum) errors.push(`"${k}" is below ${spec.minimum}`);
      if (spec.maximum !== undefined && v > spec.maximum) errors.push(`"${k}" is above ${spec.maximum}`);
    } else if (t === 'boolean') {
      if (typeof v !== 'boolean') errors.push(`"${k}" must be a boolean`);
    } else errors.push(`"${k}" has an unsupported type in its schema`);
    if (spec.enum && !spec.enum.includes(v)) errors.push(`"${k}" must be one of: ${spec.enum.join(', ')}`);
  }
  return errors.length ? { ok: false, errors } : { ok: true, args: a };
}
