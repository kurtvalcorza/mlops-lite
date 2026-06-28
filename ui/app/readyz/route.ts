// Readiness probe (004 US3, FR-040) — distinct from liveness `/healthz`. Liveness says "the Node
// process is up" (what the supervisor polls, cheap). Readiness says "the console is actually
// functional" — i.e. the BFF can reach the gateway. So "process up" is never mistaken for "works".
//
// Checks the gateway's OWN open liveness probe (no API key needed — /healthz is unauthenticated).
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const GATEWAY_URL = process.env.GATEWAY_URL ?? 'http://localhost:8080';

export async function GET() {
  try {
    const r = await fetch(`${GATEWAY_URL}/healthz`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(3000),
    });
    if (r.ok) {
      return NextResponse.json({ ready: true, service: 'ui', gateway: 'reachable' });
    }
    return NextResponse.json(
      { ready: false, service: 'ui', gateway: `unhealthy (${r.status})` },
      { status: 503 },
    );
  } catch (e) {
    return NextResponse.json(
      { ready: false, service: 'ui', gateway: `unreachable: ${String(e)}` },
      { status: 503 },
    );
  }
}
