// Client-side gateway access — always via the BFF (/api/gw/...), never the gateway directly, so the
// API key stays server-side (FR-024). These run in the browser.
//
// 018 US3 (T371): failures throw GwError carrying the STRUCTURED status + parsed body, retiring the
// brittle `msg.includes('-> 409')` string-matching (review §4.7) — callers branch on `e.status`.

const BFF = '/api/gw';

export class GwError extends Error {
  status: number;
  body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = 'GwError';
    this.status = status;
    this.body = body;
  }
}

/** The detail string from a gateway error body, when one exists. */
export function gwDetail(e: unknown): string {
  if (e instanceof GwError) {
    const b = e.body as { detail?: unknown } | null;
    if (b && typeof b === 'object' && b.detail !== undefined) {
      return typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail);
    }
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

async function throwGwError(method: string, path: string, r: Response): Promise<never> {
  let body: unknown = null;
  let text = '';
  try {
    text = await r.text();
    body = JSON.parse(text);
  } catch {
    body = text ? { detail: text.slice(0, 300) } : null;
  }
  // Keeps the legacy "-> <status>" message shape so existing logs/tests stay familiar.
  throw new GwError(`${method} ${path} -> ${r.status}: ${text.slice(0, 300)}`, r.status, body);
}

export async function gwGet<T = unknown>(path: string): Promise<T> {
  const r = await fetch(`${BFF}/${path.replace(/^\//, '')}`, { cache: 'no-store' });
  if (!r.ok) await throwGwError('GET', path, r);
  return r.json() as Promise<T>;
}

export async function gwPost<T = unknown>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BFF}/${path.replace(/^\//, '')}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!r.ok) await throwGwError('POST', path, r);
  return r.json() as Promise<T>;
}

export async function gwPut<T = unknown>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BFF}/${path.replace(/^\//, '')}`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!r.ok) await throwGwError('PUT', path, r);
  return r.json() as Promise<T>;
}

export async function gwDelete<T = unknown>(path: string): Promise<T> {
  const r = await fetch(`${BFF}/${path.replace(/^\//, '')}`, {
    method: 'DELETE',
    cache: 'no-store',
  });
  if (!r.ok) await throwGwError('DELETE', path, r);
  return r.json() as Promise<T>;
}
