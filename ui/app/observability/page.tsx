'use client';

import { Suspense, useRef } from 'react';
import Link from 'next/link';
import { DriftPanel } from '@/components/monitoring/DriftPanel';
import { HistoryList, type HistoryHandle } from '@/components/monitoring/HistoryList';
import { LabelsPanel } from '@/components/monitoring/LabelsPanel';
import { QualityPanel } from '@/components/monitoring/QualityPanel';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { AlertsView, DashboardEmbed, MetricsSummary } from '@/lib/platform-types';

// 021 T439 (FR-238..242/248): the monitoring stage — BOTH breach signals (input drift + output
// quality), both report histories, ground-truth labeling, and the one-shot retrain arm with
// cooldown as a first-class outcome. The standing policy loop lives in /retraining (US4); the
// in-page note below states the manual-vs-standing relationship explicitly (FR-248).
export default function MonitoringPage() {
  const historyRef = useRef<HistoryHandle>(null);
  const refreshHistory = () => historyRef.current?.refresh();

  return (
    <>
      <PageTitle sub="Watch what serving does to quality: run checks, read histories, attach ground truth.">
        monitoring
      </PageTitle>

      {/* FR-248: these are MANUAL, ONE-SHOT checks; the standing counterpart is declared in retraining */}
      <p className="mb-6 text-caption-md text-mute">
        [i] checks here are <span className="text-ink">manual and one-shot</span>. Their{' '}
        <span className="text-ink">standing, scheduled</span> counterpart is a per-model policy in{' '}
        <Link href="/retraining" className="st-accent underline">
          retraining
        </Link>{' '}
        — same checks, same gate, same shared cooldown.
      </p>

      {/* 027 US9 (T759): the curated native panels, rule state, and the dashboard link. */}
      <div className="mb-6 grid gap-6 lg:grid-cols-2">
        <MetricPanels />
        <Alerts />
      </div>

      <div className="mb-6">
        <Dashboards />
      </div>

      <div className="mb-6 grid gap-6 lg:grid-cols-2">
        <DriftPanel onRan={refreshHistory} />
        <QualityPanel onRan={refreshHistory} />
      </div>

      <div className="mb-6">
        {/* useSearchParams (the ?prediction_id= hand-off) needs a Suspense boundary at build time */}
        <Suspense fallback={<p className="text-caption-md text-ash">[~] loading labels…</p>}>
          <LabelsPanel />
        </Suspense>
      </div>

      <HistoryList ref={historyRef} />
    </>
  );
}

/**
 * 027 T756/T759 — the curated native panels (FR-423).
 *
 * A degraded panel shows **no points**, not zero points. A flat line at zero is a measurement
 * claim — "the request rate was zero" — and an unreachable source has not made it. This is the same
 * rule as `null` vs `0` on the Overview cards, applied to a shape where the temptation is stronger:
 * an empty chart looks broken, and drawing a baseline makes it look fine.
 */
function MetricPanels() {
  const { data, ageMs, degraded } = useLive<MetricsSummary>(
    'console/metrics/summary',
    CADENCE.health,
  );

  return (
    <Panel title="Platform metrics">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>
      {!data ? (
        <p className="text-body-md text-mute">unknown</p>
      ) : (
        <ul className="font-mono text-body-md">
          {data.panels.map((panel) => {
            const point = panel.series[0]?.points[0];
            return (
              <li key={panel.key} className="text-mute">
                {panel.key}:{' '}
                {panel.degraded || !point ? (
                  <span className="text-ash">no measurement</span>
                ) : (
                  <span className="text-ink">
                    {point[1]}
                    {panel.unit ?? ''}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/**
 * 027 T757 — alert rules, and the notice that nobody was told.
 *
 * `AlertRule` carries no delivery, notification, recipient, or acknowledgement field, and the
 * surface says so out loud. This platform has no Alertmanager: a firing rule pages nobody, and an
 * operator who assumed otherwise would not send the page themselves. The runbook link is the honest
 * substitute — it says what to DO without claiming anyone was told.
 */
function Alerts() {
  const { data, ageMs, degraded } = useLive<AlertsView>('console/alerts', CADENCE.admin);

  return (
    <Panel title="Alert rules">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>
      {!data ? (
        <p className="text-body-md text-mute">unknown</p>
      ) : data.rules.length === 0 ? (
        <p className="text-body-md text-mute">no rules configured</p>
      ) : (
        <ul className="font-mono text-body-md text-mute">
          {data.rules.map((rule) => (
            <li key={rule.name}>
              {rule.name} · <span className="text-ink">{rule.state}</span>
              {rule.severity && <span className="text-ash"> · {rule.severity}</span>}
              {rule.runbookUrl && (
                <>
                  {' · '}
                  <Link href={rule.runbookUrl} className="underline">
                    runbook
                  </Link>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-caption-md st-warning">
        [!] {data?.noDeliveryNotice ?? NO_DELIVERY_FALLBACK}
      </p>
    </Panel>
  );
}

/**
 * Shown when the alerts route is unreadable. Like the drift limitations, this is a property of the
 * DEPLOYMENT rather than of any response — an outage must not be able to remove the notice, because
 * a rules list without it reads as a working alerting system.
 */
const NO_DELIVERY_FALLBACK =
  'No notification was sent. This platform has no alert delivery channel — these are rule states ' +
  'only, and nobody is paged when one fires.';

/**
 * 027 T757/T759 — the dashboard embed (FR-425).
 *
 * `embeddable` is resolved **server-side** from the frame policy, not discovered by the browser
 * failing to render a frame — which would turn a configuration fact into a blank rectangle with no
 * way to offer the link that would have worked. `externalUrl` is always present, so the fallback is
 * a designed state rather than an error path, and the embed is labelled external because the
 * dashboard tool runs anonymous and read-only behind its own CSP.
 */
function Dashboards() {
  const { data } = useLive<DashboardEmbed[]>('console/dashboards', CADENCE.admin);

  return (
    <Panel title="Dashboards">
      {!data ? (
        <p className="text-body-md text-mute">unknown</p>
      ) : (
        <ul className="text-body-md">
          {data.map((dashboard) => (
            <li key={dashboard.id}>
              <Link href={dashboard.externalUrl} className="underline">
                {dashboard.title}
              </Link>{' '}
              <span className="text-mute">
                — external dashboard, anonymous and read-only. No administrative controls.
              </span>
              {!dashboard.embeddable && dashboard.reason && (
                <span className="text-ash"> ({dashboard.reason})</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
