'use client';

// 027 US3 (T723) — the model catalog: one list across five systems (FR-383/384).
//
// 021's models page was the registry with the promote gate as its centerpiece. That surface is not
// gone — it moved to `/models/[name]`, where it belongs, next to the versions it acts on. This page
// is the question that had no home before: *what models does this platform have, and what is true
// about each of them right now*, joined across the registry, the object store, the tracking server,
// the serving pointer, and the evaluation record.
//
// The join's rule shows up directly in what is rendered: a row whose other side is missing is
// **marked absent, never dropped**. A registry version with no artifact is exactly the row an
// operator came here to find.

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { PlatformModel } from '@/lib/platform-types';

const MODALITIES = ['', 'text-generation', 'image-classification', 'embedding', 'asr', 'tabular',
  'unknown'];
const EVALUATION_STATES = ['', 'passed', 'failed', 'warning', 'not-evaluated', 'incomplete'];

type CatalogPage = { models: PlatformModel[]; total: number; offset: number; limit: number };

export default function ModelsPage() {
  return (
    <Suspense fallback={<p className="text-caption-md text-ash">[~] loading…</p>}>
      <CatalogView />
    </Suspense>
  );
}

function CatalogView() {
  const [modality, setModality] = useState('');
  const [evaluationState, setEvaluationState] = useState('');
  const [verify, setVerify] = useState(false);

  const query = new URLSearchParams();
  if (modality) query.set('modality', modality);
  if (evaluationState) query.set('evaluation_state', evaluationState);
  // Opt-in, and labelled as such: an existence check per row is a round trip per row, so the list
  // view leaves `artifactPresent` unknown until asked rather than making the page cost grow with
  // the registry.
  if (verify) query.set('verify_artifacts', 'true');

  const { data, ageMs, degraded } = useLive<CatalogPage>(
    `console/catalog?${query.toString()}`,
    CADENCE.catalog,
  );

  return (
    <>
      <PageTitle sub="Every model this platform has, joined across the registry, the object store, tracking, deployment, and evaluation.">
        models
      </PageTitle>

      <p className="mb-3 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>

      <div className="mb-4 flex flex-wrap items-center gap-3 text-caption-md">
        <label>
          modality{' '}
          <select
            value={modality}
            onChange={(e) => setModality(e.target.value)}
            className="bg-card text-ink"
          >
            {MODALITIES.map((m) => (
              <option key={m} value={m}>
                {m || 'any'}
              </option>
            ))}
          </select>
        </label>
        <label>
          evaluation{' '}
          <select
            value={evaluationState}
            onChange={(e) => setEvaluationState(e.target.value)}
            className="bg-card text-ink"
          >
            {EVALUATION_STATES.map((s) => (
              <option key={s} value={s}>
                {s || 'any'}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />{' '}
          verify artifacts (one object-store check per row)
        </label>
      </div>

      <Panel>
        {data === null ? (
          <p className="text-body-md text-mute">unknown — the registry did not answer</p>
        ) : data.models.length === 0 ? (
          <p className="text-body-md text-mute">no models match, based on what is readable</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mono text-body-md">
              <thead className="text-ash">
                <tr>
                  <th className="pr-4 text-left">model</th>
                  <th className="pr-4 text-left">ver</th>
                  <th className="pr-4 text-left">modality</th>
                  <th className="pr-4 text-left">alias</th>
                  <th className="pr-4 text-left">evaluation</th>
                  <th className="pr-4 text-left">artifact</th>
                  <th className="pr-4 text-left">deployed</th>
                  <th className="text-left">source run</th>
                </tr>
              </thead>
              <tbody>
                {data.models.map((model) => (
                  <tr key={model.id} className="text-mute">
                    <td className="pr-4">
                      <Link
                        href={`/models/${encodeURIComponent(model.name)}/${model.version}`}
                        className="text-ink underline"
                      >
                        {model.name}
                      </Link>
                    </td>
                    <td className="pr-4">{model.version}</td>
                    {/* An unrecognized modality reads `unknown` — it is not filtered out (FR-385). */}
                    <td className="pr-4">{model.modality}</td>
                    <td className="pr-4">{model.aliases.join(',') || '—'}</td>
                    <td className="pr-4">{model.evaluationState}</td>
                    <td className="pr-4">
                      <ArtifactCell present={model.artifactPresent} />
                    </td>
                    <td className="pr-4">{model.deploymentIds.length}</td>
                    <td>
                      {model.sourceRunId ? (
                        <Link href={`/training/runs/${model.sourceRunId}`} className="underline">
                          {model.sourceRunId.slice(0, 8)}
                        </Link>
                      ) : (
                        // Marked absent, never dropped: a version with no source run is a finding.
                        <span className="text-ash">absent</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-caption-md text-ash">
              {data.models.length} of {data.total}
            </p>
          </div>
        )}
      </Panel>
    </>
  );
}

/** Three states, three words. `null` is "unchecked", which is not "missing". */
function ArtifactCell({ present }: { present: boolean | null }) {
  if (present === null) return <span className="text-ash">unchecked</span>;
  return present ? <span>present</span> : <span className="text-ink">MISSING</span>;
}
