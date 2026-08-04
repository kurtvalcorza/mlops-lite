'use client';

// 027 US5 (T739) — evaluation runs and the gate, with the evidence attached.
//
// The rule this page exists to satisfy: a gate failure must carry the rule that produced it, the
// observed value, and the incumbent it was compared against, **without leaving the view**
// (SC-191). A verdict on its own — "blocked" — sends the operator to the tracking UI to reconstruct
// why, and that round trip is exactly what this console is for removing.
//
// Metrics are modality-native and are never coerced into a common score (FR-399). There is no
// "score" column here on purpose: a WER and an accuracy in one column would rank an ASR model
// against a classifier.

import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { EvaluationResult, GateConfig } from '@/lib/platform-types';

export default function EvaluationsPage() {
  const evaluations = useLive<EvaluationResult[]>('console/evaluations', CADENCE.catalog);
  const gate = useLive<GateConfig>('console/gates', CADENCE.admin);

  return (
    <div>
      <PageTitle sub="Quality gates with their evidence, comparisons, and drift">
        Evaluations
      </PageTitle>

      <div className="flex flex-col gap-6">
        <Panel title="The gate">
          {!gate.data ? (
            <p className="text-body-md text-mute">unknown — the gate configuration is unreadable</p>
          ) : (
            <>
              <p className="font-mono text-body-md text-ink">
                mode {gate.data.mode} · tolerance {gate.data.tolerance} · missing metric:{' '}
                {gate.data.missingMetricPolicy}
              </p>
              <ul className="mt-2 text-body-md text-mute">
                {gate.data.rules.map((rule) => (
                  <li key={rule.metric}>
                    {rule.metric} — {rule.operator}
                    {rule.scope && ` (${rule.scope})`}
                  </li>
                ))}
              </ul>
            </>
          )}
        </Panel>

        <Panel title="Evaluation runs">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(evaluations.ageMs)}
            {evaluations.degraded.length > 0 &&
              ` · unreachable: ${evaluations.degraded.join(', ')}`}
          </p>
          {evaluations.data === null ? (
            <p className="text-body-md text-mute">unknown — the registry did not answer</p>
          ) : evaluations.data.length === 0 ? (
            <p className="text-body-md text-mute">nothing recorded</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-body-md">
                <thead className="text-ash">
                  <tr>
                    <th className="pr-4 text-left">model</th>
                    <th className="pr-4 text-left">ver</th>
                    <th className="pr-4 text-left">modality</th>
                    {/* Metric NAME and direction, per row. Never a shared "score" column. */}
                    <th className="pr-4 text-left">metric</th>
                    <th className="pr-4 text-left">value</th>
                    <th className="text-left">gate</th>
                  </tr>
                </thead>
                <tbody>
                  {evaluations.data.map((result) => (
                    <tr key={result.id} className="text-mute">
                      <td className="pr-4">
                        <Link
                          href={`/evaluations/${encodeURIComponent(result.modelName)}/${result.version}`}
                          className="text-ink underline"
                        >
                          {result.modelName}
                        </Link>
                      </td>
                      <td className="pr-4">{result.version}</td>
                      <td className="pr-4">{result.modality ?? 'unknown'}</td>
                      <td className="pr-4">{result.metrics[0]?.name ?? '—'}</td>
                      <td className="pr-4">
                        {result.metrics[0] ? (
                          <>
                            {result.metrics[0].value}{' '}
                            <span className="text-ash">
                              ({result.metrics[0].direction === 'higher-better' ? '↑' : '↓'})
                            </span>
                          </>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className={result.gate.outcome === 'failed' ? 'text-ink' : ''}>
                        {result.gate.outcome}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title="Elsewhere">
          <ul className="text-body-md">
            <li>
              <Link href="/evaluations/drift" className="underline">
                Drift
              </Link>{' '}
              <span className="text-mute">— reports, thresholds, and the retrain cycle</span>
            </li>
          </ul>
        </Panel>
      </div>
    </div>
  );
}
