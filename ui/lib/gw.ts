// Client-side gateway access — always via the BFF (/api/gw/...), never the gateway directly, so the
// API key stays server-side (FR-024). These run in the browser.

const BFF = '/api/gw';

export async function gwGet<T = unknown>(path: string): Promise<T> {
  const r = await fetch(`${BFF}/${path.replace(/^\//, '')}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status}: ${await safeText(r)}`);
  return r.json() as Promise<T>;
}

export async function gwPost<T = unknown>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BFF}/${path.replace(/^\//, '')}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!r.ok) throw new Error(`POST ${path} -> ${r.status}: ${await safeText(r)}`);
  return r.json() as Promise<T>;
}

async function safeText(r: Response): Promise<string> {
  try {
    return (await r.text()).slice(0, 300);
  } catch {
    return '(no body)';
  }
}
