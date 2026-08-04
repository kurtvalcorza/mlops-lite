'use client';

// 027 US5 (T739) — one evaluation, and the comparison workspace.
//
// SC-191 in one screen: the failing rule, the observed value, the incumbent it was compared against,
// and any override with its recorded reason. Nothing here requires leaving for the tracking UI.
//
// The comparison keeps its six dimensions **separate** (FR-403). There is no combined verdict,
// because a challenger that wins on quality and loses on latency is a trade-off the operator has to
// make — and a single "better" would make it for them silently.

import { useParams } from 'next/navigation';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { ThresholdBar } from '@/lib/charts';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { ComparisonView, EvaluationResult } from '@/lib/platform-types';

export default function EvaluationPage() {
  const routeParams = useParams<{ name: string; version: string }>();
  const name = decodeURIComponent(String(routeParams.name));
  const version = String(routeParams.version);

  const result = useLive<EvaluationResult>(
    `console/evaluations/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
    CADENCE.catalog,
  );
  const compare = useLive<ComparisonView>(
    `console/compare?name=${encodeURIComponent(name)}&challenger=${encodeURIComponent(version)}`,
    CADENCE.catalog,
  );

  const gate = result.data?.gate;

  return (
    <>
      <PageTitle sub="The verdict, and the evidence behind it.">
        {name} v{version}
      </PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/evaluations" className="underline text-mute">
          ← evaluations
        </Link>
        {' · '}
        <Link href={`/models/${encodeURIComponent(name)}/${version}`} className="underline text-mute">
          model
        </Link>
      </p>

      <div className="flex flex-col gap-6">
        <Panel title="Gate">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(result.ageMs)}
            {result.degraded.length > 0 && ` · unreachable: ${result.degraded.join(', ')}`}
          </p>
          {!gate ? (
            <p className="text-body-md text-mute">unknown — no verdict could be read</p>
          ) : (
            <>
              <p className="text-body-md text-ink">
                <span className="font-mono">{gate.outcome}</span>
                {gate.reason && <span className="text-mute"> — {gate.reason}</span>}
              </p>

              {gate.failedRule ? (
                <>
                  {/* The evidence, all of it, right here. */}
                  <dl className="mt-3 font-mono text-body-md">
                    <Row label="rule" value={`${gate.failedRule.metric} ${gate.failedRule.operator} ${gate.failedRule.threshold}`} />
                    <Row label="observed" value={String(gate.observedValue)} />
                    <Row
                      label="compared against"
                      value={
                        gate.comparedAgainst
                          ? `v${gate.comparedAgainst.version} at ${gate.comparedAgainst.value}`
                          : 'no incumbent'
                      }
                    />
                    <Row label="delta" value={gate.delta === null ? 'unknown' : String(gate.delta)} />
                    <Row label="mode" value={`${gate.mode} · tolerance ${gate.tolerance}`} />
                  </dl>
                  {gate.observedValue !== null && gate.failedRule.threshold !== null && (
                    <div className="mt-3 text-ink">
                      {/* The threshold is DRAWN, never implied by colour alone. */}
                      <ThresholdBar
                        value={gate.observedValue}
                        threshold={gate.failedRule.threshold}
                        label="observed against threshold"
                      />
                    </div>
                  )}
                </>
              ) : (
                <p className="mt-2 text-body-md text-mute">
                  no failing rule — nothing was blocked
                </p>
              )}

              {gate.override.applied && (
                <p className="mt-3 text-body-md st-warning">
                  [!] overridden —{' '}
                  {/* FR-401: an override with no reason is indistinguishable from a gate that was
                      never enforced, so the reason is shown wherever the flag is. */}
                  {gate.override.reason ?? 'NO REASON RECORDED'}
                </p>
              )}
            </>
          )}
        </Panel>

        <Panel title="Metrics">
          {!result.data || result.data.metrics.length === 0 ? (
            <p className="text-body-md text-mute">not evaluated</p>
          ) : (
            <ul className="font-mono text-body-md">
              {result.data.metrics.map((metric) => (
                <li key={metric.name}>
                  {metric.name}: {metric.value}{' '}
                  <span className="text-ash">({metric.direction})</span>
                </li>
              ))}
            </ul>
          )}
          {result.data?.benchmarkName && (
            <p className="mt-2 text-caption-md text-ash">
              benchmark {result.data.benchmarkName}
              {result.data.benchmarkDigest && ` · ${result.data.benchmarkDigest.slice(0, 12)}`}
            </p>
          )}
        </Panel>

        <Panel title="Champion / challenger">
          {!compare.data ? (
            <p className="text-body-md text-mute">unknown — the comparison could not be computed</p>
          ) : (
            <dl className="font-mono text-body-md">
              {/* Six dimensions, six rows. No combined verdict. */}
              <Dimension
                label="quality"
                challenger={
                  compare.data.quality.challenger
                    ? `${compare.data.quality.challenger.name} ${compare.data.quality.challenger.value}`
                    : null
                }
                champion={
                  compare.data.quality.champion
                    ? `${compare.data.quality.champion.name} ${compare.data.quality.champion.value}`
                    : null
                }
              />
              <Dimension label="latency" challenger={null} champion={null} />
              <Dimension label="resources" challenger={null} champion={null} />
              <Dimension
                label="artifacts"
                challenger={compare.data.artifacts.challenger}
                champion={compare.data.artifacts.champion}
              />
              <Dimension
                label="datasets"
                challenger={compare.data.datasets.challenger}
                champion={compare.data.datasets.champion}
              />
              <Dimension label="policy" challenger={compare.data.policy.gate.outcome} champion={null} />
            </dl>
          )}
        </Panel>
      </div>
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3">
      <dt className="w-40 shrink-0 text-ash">{label}</dt>
      <dd className="text-ink">{value}</dd>
    </div>
  );
}

/** `null` renders as "not measured", never as a zero — a zero would invite comparison. */
function Dimension({
  label,
  challenger,
  champion,
}: {
  label: string;
  challenger: string | null;
  champion: string | null;
}) {
  return (
    <div className="flex gap-3">
      <dt className="w-28 shrink-0 text-ash">{label}</dt>
      <dd className="text-ink">
        {challenger ?? <span className="text-ash">not measured</span>}
        <span className="text-ash"> vs </span>
        {champion ?? <span className="text-ash">not measured</span>}
      </dd>
    </div>
  );
}
