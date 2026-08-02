'use client';

// 027 US4 — one tracking run. Tracking vocabulary is preserved VERBATIM (FR-366): a run is a run,
// an experiment is an experiment, a metric is a metric. Renaming them into console-proprietary
// equivalents would mean an operator reading the tracking UI and the console side by side has to
// translate between two names for the same thing, which is the cost the rename was supposed to save.

import { useParams } from 'next/navigation';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { TrackingRun } from '@/lib/platform-types';

export default function RunPage() {
  const routeParams = useParams<{ id: string }>();
  const id = decodeURIComponent(String(routeParams.id));
  const { data, ageMs, degraded } = useLive<TrackingRun[]>('console/runs?limit=200', CADENCE.jobs);
  const run = data?.find((r) => r.run_id === id) ?? null;

  return (
    <>
      <PageTitle sub="Tracking run — parameters, metrics, and the experiment it belongs to.">
        run {id.slice(0, 12)}
      </PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/training" className="underline text-mute">
          ← training
        </Link>
      </p>

      <Panel title="Run">
        <p className="mb-2 text-caption-md text-ash">
          {formatAge(ageMs)}
          {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
        </p>
        {data === null ? (
          <p className="text-body-md text-mute">unknown — the tracking server did not answer</p>
        ) : !run ? (
          <p className="text-body-md text-mute">no run with this id in the readable window</p>
        ) : (
          <>
            <dl className="font-mono text-body-md">
              <Row label="name" value={run.name} />
              <Row label="status" value={run.status} />
              <Row label="experiment" value={run.experiment_name} />
              <Row label="job" value={run.job_id} />
            </dl>
            <Metrics title="metrics" values={run.metrics} />
            <Metrics title="params" values={run.params} />
            {run.job_id && (
              <p className="mt-3 text-body-md">
                <Link href={`/training/jobs/${run.job_id}`} className="underline">
                  the job that produced this run
                </Link>
              </p>
            )}
          </>
        )}
      </Panel>
    </>
  );
}

function Row({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="flex gap-3">
      <dt className="w-32 shrink-0 text-ash">{label}</dt>
      <dd className="text-ink">{value ?? 'unknown'}</dd>
    </div>
  );
}

function Metrics({ title, values }: { title: string; values: Record<string, unknown> }) {
  const entries = Object.entries(values ?? {});
  if (entries.length === 0) return null;
  return (
    <>
      <p className="mt-3 text-caption-md text-ash">{title}</p>
      <dl className="font-mono text-body-md">
        {entries.map(([key, value]) => (
          <div key={key} className="flex gap-3">
            <dt className="w-48 shrink-0 text-ash">{key}</dt>
            <dd className="text-ink">{String(value)}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}
