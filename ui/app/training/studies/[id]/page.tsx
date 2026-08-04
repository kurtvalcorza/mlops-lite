'use client';

// 027 US4 (T728) — an HPO study: trials, objective history, parameter importance (FR-396).
//
// Everything on this page is past tense, and that is a requirement rather than a style choice
// (FR-397). There is no persistent search service on this platform: `POST /studies` runs N trainings
// sequentially and returns, and nothing keeps optimizing afterwards. A view that spoke in the
// present tense — "exploring", "next trial", "converging" — would invite an operator to wait for
// something nobody scheduled.
//
// The parallel-coordinates and history charts are the hand-rolled SVG primitives (SC-198): no
// charting dependency for four shapes.

import { useParams } from 'next/navigation';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { ParallelCoordinates, Sparkline } from '@/lib/charts';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { StudyTrials } from '@/lib/platform-types';

export default function StudyPage() {
  const routeParams = useParams<{ id: string }>();
  const id = decodeURIComponent(String(routeParams.id));
  const { data, ageMs, degraded } = useLive<StudyTrials>(
    `console/studies/${encodeURIComponent(id)}/trials`,
    CADENCE.jobs,
  );

  return (
    <>
      <PageTitle sub="Recorded trials from a completed sequence of trainings — not a running search.">
        study {id}
      </PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/training" className="underline text-mute">
          ← training
        </Link>
      </p>

      <div className="flex flex-col gap-6">
        <Panel title="Study">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(ageMs)}
            {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
          </p>
          {!data ? (
            <p className="text-body-md text-mute">unknown — the trainer did not answer</p>
          ) : (
            <>
              <p className="font-mono text-body-md text-ink">
                {data.completed} of {data.recorded} recorded trial(s) produced a score · metric{' '}
                {data.metric ?? 'unknown'} · {data.status}
              </p>
              {data.best && (
                <p className="mt-2 font-mono text-body-md">
                  best: v{data.best.version} at {data.best.value}
                </p>
              )}
            </>
          )}
        </Panel>

        <Panel title="Objective history">
          {!data || data.history.length === 0 ? (
            <p className="text-body-md text-mute">no scored trials</p>
          ) : (
            <div className="text-ink">
              {/* Recorded values only, not smoothed and not extrapolated — a fitted curve over
                  eight points would be a prediction the data cannot support. */}
              <Sparkline
                values={data.history.map((h) => h.value)}
                width={320}
                height={60}
                label="objective by trial"
              />
            </div>
          )}
        </Panel>

        <Panel title="Trials">
          {!data || data.trials.length === 0 ? (
            <p className="text-body-md text-mute">no trials recorded</p>
          ) : (
            <>
              <div className="mb-3 text-ink">
                <ParallelCoordinates
                  axes={data.axes}
                  rows={data.trials.map((t) =>
                    data.axes.map((axis) => {
                      const value = t.params[axis];
                      return typeof value === 'number' ? value : null;
                    }),
                  )}
                  label="trials across hyperparameter axes"
                />
              </div>
              <div className="overflow-x-auto">
                <table className="w-full font-mono text-body-md">
                  <thead className="text-ash">
                    <tr>
                      <th className="pr-4 text-left">#</th>
                      <th className="pr-4 text-left">value</th>
                      <th className="pr-4 text-left">state</th>
                      {data.axes.map((axis) => (
                        <th key={axis} className="pr-4 text-left">
                          {axis}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.trials.map((trial) => (
                      <tr key={trial.number} className={trial.failed ? 'text-ash' : 'text-mute'}>
                        <td className="pr-4">{trial.number}</td>
                        {/* A trial that produced no model is FAILED, not scored worst — scoring it
                            worst would let a crash masquerade as a bad hyperparameter choice. */}
                        <td className="pr-4">{trial.value ?? 'failed'}</td>
                        <td className="pr-4">{trial.state}</td>
                        {data.axes.map((axis) => (
                          <td key={axis} className="pr-4">
                            {String(trial.params[axis] ?? '—')}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Panel>

        <Panel title="Parameter importance">
          {!data || Object.keys(data.importance).length === 0 ? (
            <p className="text-body-md text-mute">
              too few scored trials to correlate — no importance is claimed
            </p>
          ) : (
            <ul className="font-mono text-body-md">
              {Object.entries(data.importance).map(([name, entry]) => (
                <li key={name}>
                  {name}: {entry.correlation}{' '}
                  {/* The trial count is shown next to every number: an importance from four trials
                      and one from four hundred are not the same claim. */}
                  <span className="text-ash">(rank correlation over {entry.trials} trials)</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}
