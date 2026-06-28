// Liveness probe (003 US1, FR-031) — what the US2 supervisor polls to manage the ui daemon.
// Deliberately self-contained (no gateway round-trip): it answers whether the Next process is up.
export const dynamic = 'force-dynamic';

export function GET() {
  return Response.json({ status: 'ok', service: 'ui' });
}
