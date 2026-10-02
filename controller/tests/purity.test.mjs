import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const files = [];
(function walk(d) { for (const f of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, f.name); if (f.isDirectory()) { if (f.name !== 'tests' && f.name !== 'node_modules') walk(p); } else if (f.name.endsWith('.js')) files.push(p); } })(root);

test('the controller package has no DOM, simulator, UI, or global-state dependencies', () => {
  assert.ok(files.length >= 10);
  for (const f of files) {
    const src = fs.readFileSync(f, 'utf8').replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
    assert.ok(!/\b(document|window|localStorage|sessionStorage)\s*[.[]/.test(src), `${path.relative(root, f)} touches a browser global`);
    for (const m of src.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
      const spec = m[1];
      if (spec.startsWith('node:')) assert.fail(`${path.relative(root, f)} imports ${spec}; connectors must stay storage/driver-agnostic`);
      if (spec.startsWith('.')) assert.ok(path.resolve(path.dirname(f), spec).startsWith(root), `${path.relative(root, f)} imports outside the package: ${spec}`);
      else assert.fail(`${path.relative(root, f)} imports a third-party module: ${spec}`);
    }
  }
});
