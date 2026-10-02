// Persistence: the controller's whole state is plain JSON, so durability is
// "write this string somewhere safe". Writes are atomic (temp file + rename)
// and the file is owner-only, since it holds operational data.

import fs from 'node:fs';
import path from 'node:path';

export function createFileStore(file) {
  const abs = path.resolve(file);
  return {
    file: abs,
    load() {
      try { return fs.readFileSync(abs, 'utf8'); }
      catch (e) { if (e.code === 'ENOENT') return null; throw e; }
    },
    save(text) {
      fs.mkdirSync(path.dirname(abs), { recursive: true });
      const tmp = abs + '.tmp';
      fs.writeFileSync(tmp, text, { mode: 0o600 });
      fs.renameSync(tmp, abs);
    }
  };
}

// Saves whenever something durable changed. Almost everything that changes
// state writes an audit line, so the audit sequence number is the change
// signal; mutations that don't (a reviewer's pick) call touch().
export function autosave({ controller, store, intervalMs = 1000, onError = () => {} }) {
  let last = null;
  const sig = () => { const s = controller.state; return `${s.seq}:${s.batches}:${s.reviews.length}:${s.bindings.length}`; };
  const flush = () => { const s = sig(); if (s === last) return false; store.save(controller.snapshot()); last = s; return true; };
  const timer = setInterval(() => { try { flush(); } catch (e) { onError(e); } }, intervalMs);
  timer.unref();
  return { flush, touch() { last = null; }, markClean() { last = sig(); }, stop() { clearInterval(timer); try { flush(); } catch (e) { onError(e); } } };
}
