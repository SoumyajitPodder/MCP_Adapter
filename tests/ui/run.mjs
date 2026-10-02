// Runs every browser-level suite from the project root (they read ./index.html
// relative to it) and fails if any reports a failure.
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const suites = ['test_coldstart', 'test_file_api', 'test_needsmapping', 'full_test', 'verify2'];
let bad = 0;
for (const s of suites) {
  const r = spawnSync(process.execPath, [path.join('tests/ui', s + '.mjs')], { cwd: root, encoding: 'utf8', timeout: 300000 });
  const out = (r.stdout || '') + (r.stderr || '');
  const failed = r.status !== 0 || /FAIL:/.test(out) || /\b[1-9]\d* failed/.test(out) || /any window errors: true/.test(out);
  const last = out.split('\n').filter(l => /passed|window errors/.test(l)).pop() || '(no summary line)';
  console.log(`${failed ? '✗' : '✓'} ${s.padEnd(18)} ${last.trim()}`);
  if (failed) { bad++; console.log(out.split('\n').filter(l => /FAIL|Error/.test(l)).slice(0, 6).join('\n')); }
}
process.exit(bad ? 1 : 0);
