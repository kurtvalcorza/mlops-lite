'use client';

import { useEffect, useRef, useState } from 'react';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';

type DaemonState = { reachable: boolean; url: string };
type StateSnap = {
  all_healthy?: boolean;
  daemons?: Record<string, DaemonState>;
  serving?: { resident?: boolean; est_vram_gb?: number; fits?: boolean; vram_budget_gb?: number } | null;
  gpu_free?: number | null;
};

// Browser-side: the Grafana host port (anonymous + embedding enabled). localhost resolves to the
// host from the Windows browser viewing the UI on 127.0.0.1.
const GRAFANA =
  process.env.NEXT_PUBLIC_GRAFANA_URL ?? 'http://localhost:3001';
const GRAFANA_SRC = `${GRAFANA}/d/mlops-lite/mlops-lite-platform?kiosk&theme=light&refresh=10s`;

export default function HealthPage() {
  const [snap, setSnap] = useState<StateSnap | null>(null);
  const [connected, setConnected] = useState(false);
  const [uiReady, setUiReady] = useState<boolean | null>(null);
  const esRef = useRef<EventSource | null>(null);

  // Console readiness (004 US3): /readyz reflects whether the BFF can reach the gateway — distinct
  // from the liveness that lets this page render at all. Poll it alongside the live state channel.
  useEffect(() => {
    let alive = true;
    const tick = () =>
      fetch('/readyz', { cache: 'no-store' })
        .then((r) => alive && setUiReady(r.ok))
        .catch(() => alive && setUiReady(false));
    tick();
    const id = setInterval(tick, 4000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    // GET SSE via the BFF (EventSource is GET-only; the BFF injects the API key server-side).
    const es = new EventSource('/api/gw/platform/events');
    esRef.current = es;
    es.onopen = () => setConnected(true);
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.event === 'state') setSnap(data);
      } catch {
        /* ignore keep-alives */
      }
    };
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, []);

  const daemons = snap?.daemons ?? {};
  // The ui itself isn't a gateway-proxied daemon — if this page renders, the ui process is live; the
  // tile reports READINESS (BFF→gateway reachable via /readyz), not just process liveness.
  const tiles: { name: string; ok: boolean; detail?: string }[] = [
    {
      name: 'ui',
      ok: uiReady !== false,
      detail: uiReady === null ? 'this console' : uiReady ? 'ready (gateway reachable)' : 'not ready (gateway unreachable)',
    },
    ...Object.entries(daemons).map(([name, d]) => ({ name, ok: d.reachable })),
  ];

  return (
    <>
      <PageTitle sub="Live daemon + GPU state via SSE, with embedded Grafana history.">
        health
      </PageTitle>

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {tiles.map((t) => (
          <Panel key={t.name}>
            <div className="flex items-center justify-between">
              <span className="text-heading-md text-ink">{t.name}</span>
              <Badge tone={t.ok ? 'success' : 'danger'}>{t.ok ? 'healthy' : 'unreachable'}</Badge>
            </div>
            {t.detail && <p className="mt-1 text-caption-md text-ash">{t.detail}</p>}
          </Panel>
        ))}

        {/* GPU / serving chart-tile (sparse ASCII figures, not a chart lib) */}
        <Panel title="gpu" hint="single-GPU, on-demand">
          <dl className="space-y-1 text-body-md">
            <Row k="free vram">
              {snap?.gpu_free != null ? `${snap.gpu_free} MiB` : '—'}
            </Row>
            <Row k="model resident">
              {snap?.serving?.resident == null ? (
                '—'
              ) : snap.serving.resident ? (
                <span className="st-accent">[~] loaded</span>
              ) : (
                <span className="st-mute">[ ] released</span>
              )}
            </Row>
            <Row k="est. footprint">
              {snap?.serving?.est_vram_gb != null
                ? `${snap.serving.est_vram_gb} / ${snap.serving.vram_budget_gb} GB`
                : '—'}
            </Row>
          </dl>
        </Panel>
      </div>

      <div className="mb-3 flex items-center gap-2 text-caption-md">
        <Badge tone={connected ? 'success' : 'warning'} />
        <span className="text-mute">
          {connected ? 'live — SSE state channel connected' : 'reconnecting to state channel…'}
        </span>
      </div>

      <Panel title="grafana" hint="embedded history (Prometheus)">
        <iframe
          src={GRAFANA_SRC}
          className="hairline h-[640px] w-full rounded-none bg-canvas"
          title="MLOps-Lite Grafana dashboard"
        />
        <p className="mt-2 text-caption-md text-ash">
          [i] panels served by Grafana on {GRAFANA}. If blank, the dashboard is still provisioning.
        </p>
      </Panel>
    </>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-mute">{k}</dt>
      <dd className="text-ink">{children}</dd>
    </div>
  );
}
