// BFF proxy (003 US1 + 004 US1, T080/T081). The browser talks ONLY to this route; the gateway API
// key lives here, server-side, injected as X-API-Key, never sent to the client (FR-024). Hardened:
//   - the key is injected ONLY for an explicit allowlist of gateway routes (FR-032) — any other
//     path/method is rejected BEFORE the key is attached, so the key can't ride a non-console call;
//   - cross-origin / non-localhost callers are rejected (FR-033) so a foreign page can't use the BFF
//     as a confused deputy (CSRF) to drive promote/launch-run/retrain with the operator key.
// REST and SSE (text/event-stream) are both proxied by streaming the upstream body through.
import { NextRequest } from 'next/server';
import { isAllowed } from '@/lib/gw-allowlist';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const GATEWAY_URL = process.env.GATEWAY_URL ?? 'http://localhost:8080';
const API_KEY = process.env.GATEWAY_API_KEY ?? '';
const UI_PORT = process.env.UI_PORT ?? '3000';

// Extra origins an operator may explicitly allow (005 US4, FR-044). Comma-separated absolute
// origins (e.g. "http://workstation.local:3000"). Each is parsed with the URL API: a malformed or
// non-http(s) entry (including "*") is DROPPED, never widening the guard to allow-all.
function parseExtraOrigins(raw: string | undefined): { hosts: string[]; origins: string[] } {
  const hosts: string[] = [];
  const origins: string[] = [];
  for (const entry of (raw ?? '').split(',')) {
    const s = entry.trim();
    if (!s) continue;
    let u: URL;
    try {
      u = new URL(s);
    } catch {
      continue; // malformed — ignore, never widen
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') continue;
    origins.push(u.origin); // normalized scheme://host[:port]
    hosts.push(u.host); // host[:port]
  }
  return { hosts, origins };
}

const EXTRA = parseExtraOrigins(process.env.UI_ALLOWED_ORIGINS);

// The only origins/hosts the console legitimately runs on (localhost-bound, FR-025) — IPv4 + IPv6
// loopback by default, plus any explicitly configured extra origins (FR-044).
const ALLOWED_HOSTS = new Set([
  `127.0.0.1:${UI_PORT}`, `localhost:${UI_PORT}`, `[::1]:${UI_PORT}`, ...EXTRA.hosts,
]);
const ALLOWED_ORIGINS = new Set([
  `http://127.0.0.1:${UI_PORT}`, `http://localhost:${UI_PORT}`, `http://[::1]:${UI_PORT}`,
  ...EXTRA.origins,
]);

// Hop-by-hop / identity headers we must not forward (host/length recomputed; any inbound key dropped
// — only our server-side key is injected).
const STRIP = new Set([
  'host', 'connection', 'content-length', 'x-api-key', 'cookie', 'accept-encoding', 'origin',
]);

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Same-origin / localhost guard (FR-033): reject foreign Origin or non-localhost Host. */
function originOk(req: NextRequest): boolean {
  const host = req.headers.get('host');
  if (!host || !ALLOWED_HOSTS.has(host)) return false;
  const origin = req.headers.get('origin');
  // Origin is absent on top-level GET navigations (fine); when present it MUST be our own.
  if (origin && !ALLOWED_ORIGINS.has(origin)) return false;
  // Belt-and-suspenders: if the browser tells us it's cross-site, refuse.
  const site = req.headers.get('sec-fetch-site');
  if (site && site !== 'same-origin' && site !== 'none') return false;
  return true;
}

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  // 1. Origin/Host guard — before anything touches the key.
  if (!originOk(req)) return json(403, { error: 'forbidden: cross-origin or non-localhost request' });

  // 2. Allowlist — the key is injected ONLY for routes the console actually uses.
  const { path } = await ctx.params;
  if (!isAllowed(req.method, path)) {
    return json(404, { error: `not proxied: ${req.method} /${path.join('/')}` });
  }

  const search = req.nextUrl.search;
  const target = `${GATEWAY_URL}/${path.join('/')}${search}`;

  const headers = new Headers();
  req.headers.forEach((v, k) => {
    if (!STRIP.has(k.toLowerCase())) headers.set(k, v);
  });
  if (API_KEY) headers.set('X-API-Key', API_KEY);

  const method = req.method.toUpperCase();
  const hasBody = method !== 'GET' && method !== 'HEAD';

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
      cache: 'no-store',
      // @ts-expect-error: Node fetch needs duplex for streaming request bodies; harmless for GET.
      duplex: 'half',
    });
  } catch (e) {
    // Non-leaky (FR-045): the upstream URL/host/port stays in server logs, never in the client body.
    console.error(`[bff] gateway unreachable at ${GATEWAY_URL}:`, e);
    return json(502, { error: 'gateway unreachable' });
  }

  // Re-emit upstream status + body, preserving content-type (SSE or JSON) and disabling buffering.
  const respHeaders = new Headers();
  const ct = upstream.headers.get('content-type');
  if (ct) respHeaders.set('content-type', ct);
  respHeaders.set('cache-control', 'no-cache, no-transform');
  respHeaders.set('x-accel-buffering', 'no');

  return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
