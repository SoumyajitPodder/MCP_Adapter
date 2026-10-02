// Small HTTP helpers shared by the routes.

export class HttpError extends Error {
  constructor(status, code, message, extra) { super(message); this.status = status; this.code = code; this.extra = extra; }
}

export function send(res, status, body, headers = {}) {
  const text = body === undefined ? '' : JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(text), ...headers });
  res.end(text);
}

// Reads a JSON body, refusing anything that isn't application/json (so a
// cross-site form posting text/plain can't reach a state-changing route) and
// anything over the size limit. No body at all reads as {}.
export async function readJson(req, limit) {
  const declared = Number(req.headers['content-length'] || 0);
  const chunked = (req.headers['transfer-encoding'] || '').includes('chunked');
  if (!declared && !chunked) return {};
  const type = (req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  if (type !== 'application/json') throw new HttpError(415, 'UNSUPPORTED_MEDIA_TYPE', 'Content-Type must be application/json');
  if (declared > limit) throw new HttpError(413, 'PAYLOAD_TOO_LARGE', `body exceeds ${limit} bytes`);
  const chunks = []; let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw new HttpError(413, 'PAYLOAD_TOO_LARGE', `body exceeds ${limit} bytes`);
    chunks.push(c);
  }
  if (!size) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new HttpError(400, 'BAD_JSON', 'body is not valid JSON'); }
}
