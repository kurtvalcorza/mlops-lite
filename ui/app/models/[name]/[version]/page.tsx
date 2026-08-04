'use client';

// 027 US3 (T723) — the model version detail: nine tabs (FR-386) and the compatibility panel.
//
// The compatibility panel is the reason this page exists. "Can this model run here right now" was
// previously answerable only by trying it and reading the refusal, and the refusal is the worst
// place to learn that the answer was structural — an operator who waits for a model that exceeds
// the budget on an empty GPU is waiting for something that will never happen.
//
// So the panel renders the verdict's THREE-way distinction explicitly, and renders BOTH VRAM checks
// separately with their own reservation terms. Merging them into "not enough VRAM" would send the
// operator to the wrong remedy: eviction fixes a budget failure and does nothing for a live-VRAM
// failure, which usually means a leaked or unaccounted allocation.

import { useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { PlatformModel, RuntimeCompatibility } from '@/lib/platform-types';

/** FR-386, verbatim and in order. */
const TABS = ['Overview', 'Versions', 'Evaluations', 'Deployments', 'Training', 'Inference',
  'Artifacts', 'Lineage', 'Activity'] as const;

type Tab = (typeof TABS)[number];

export default function VersionPage() {
  const routeParams = useParams<{ name: string; version: string }>();
  const name = decodeURIComponent(String(routeParams.name));
  const version = String(routeParams.version);
  const [tab, setTab] = useState<Tab>('Overview');

  const path = `console/catalog/${encodeURIComponent(name)}/${encodeURIComponent(version)}`;
  const model = useLive<PlatformModel>(path, CADENCE.catalog);
  // Compatibility polls at the RUNTIME cadence, not the catalog's: it is a statement about now, and
  // the topology under it moves in seconds.
  const compat = useLive<RuntimeCompatibility>(`${path}/compatibility`, CADENCE.runtime);

  return (
    <>
      <PageTitle sub="Registry, artifact, evaluation, deployment, lineage — and whether it can run here right now.">
        {name} v{version}
      </PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/models" className="underline text-mute">
          ← catalog
        </Link>
        {' · '}
        <Link href={`/models/${encodeURIComponent(name)}`} className="underline text-mute">
          all versions
        </Link>
      </p>

      <Compatibility state={compat} />

      <nav className="my-4 flex flex-wrap gap-1 text-caption-md" aria-label="model detail">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={
              'rounded-sm px-2 py-0.5 ' +
              (t === tab ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
            }
          >
            [{t === tab ? '*' : ' '}] {t.toLowerCase()}
          </button>
        ))}
      </nav>

      <Panel title={tab}>
        <p className="mb-2 text-caption-md text-ash">
          {formatAge(model.ageMs)}
          {model.degraded.length > 0 && ` · unreachable: ${model.degraded.join(', ')}`}
        </p>
        {!model.data ? (
          <p className="text-body-md text-mute">unknown — the registry did not answer</p>
        ) : (
          <TabBody tab={tab} model={model.data} name={name} version={version} />
        )}
      </Panel>
    </>
  );
}

function TabBody({
  tab,
  model,
  name,
  version,
}: {
  tab: Tab;
  model: PlatformModel;
  name: string;
  version: string;
}) {
  switch (tab) {
    case 'Overview':
      return (
        <Fields
          rows={[
            ['modality', model.modality],
            ['aliases', model.aliases.join(', ') || 'none'],
            ['evaluation', model.evaluationState],
            ['deployments', String(model.deploymentIds.length)],
            ['source run', model.sourceRunId ?? 'absent'],
          ]}
        />
      );
    case 'Versions':
      return (
        <p className="text-body-md">
          <Link href={`/models/${encodeURIComponent(name)}`} className="underline">
            every version of {name}
          </Link>{' '}
          <span className="text-mute">— with the promote gate</span>
        </p>
      );
    case 'Evaluations':
      return (
        <p className="text-body-md">
          <Link href={`/evaluations?model=${encodeURIComponent(name)}`} className="underline">
            evaluation runs for {name}
          </Link>{' '}
          <span className="text-mute">— currently {model.evaluationState}</span>
        </p>
      );
    case 'Deployments':
      return model.deploymentIds.length === 0 ? (
        <p className="text-body-md text-mute">not deployed</p>
      ) : (
        <ul className="font-mono text-body-md">
          {model.deploymentIds.map((id) => (
            <li key={id}>
              <Link href={`/deployments/${encodeURIComponent(id)}`} className="underline">
                {id}
              </Link>
            </li>
          ))}
        </ul>
      );
    case 'Training':
      return model.sourceRunId ? (
        <Link href={`/training/runs/${model.sourceRunId}`} className="underline text-body-md">
          the run that produced this version
        </Link>
      ) : (
        <p className="text-body-md text-mute">no source run recorded</p>
      );
    case 'Inference':
      return (
        <p className="text-body-md">
          <Link href={`/inference?model=${encodeURIComponent(name)}`} className="underline">
            predictions served by {name}
          </Link>
        </p>
      );
    case 'Artifacts':
      return (
        <Fields
          rows={[
            ['uri', model.artifactUri ?? 'absent'],
            ['digest', model.artifactDigest ?? 'not recorded'],
            [
              'size',
              model.artifactSizeBytes === null || model.artifactSizeBytes === undefined
                ? 'unknown'
                : `${model.artifactSizeBytes} bytes`,
            ],
            [
              'present',
              // Three states, three words. `null` is "unchecked", which is not "missing" — an
              // unchecked artifact is not a missing one.
              model.artifactPresent === null
                ? 'unchecked'
                : model.artifactPresent
                  ? 'present'
                  : 'MISSING',
            ],
          ]}
        />
      );
    case 'Lineage':
      return (
        <>
          <Fields
            rows={[
              ['base model', model.lineage?.baseModel ?? 'none (not an adapter)'],
              [
                'base resolvable',
                model.lineage?.baseResolvable === false
                  ? 'NO — this version is not servable (FR-389)'
                  : 'yes',
              ],
              ['parent run', model.lineage?.parentRunId ?? 'absent'],
            ]}
          />
          {model.lineage?.baseModel && (
            // Navigable back through the chain (FR-390): the point of lineage is following it.
            <p className="mt-2 text-body-md">
              <Link
                href={`/models/${encodeURIComponent(model.lineage.baseModel)}`}
                className="underline"
              >
                open {model.lineage.baseModel}
              </Link>
            </p>
          )}
        </>
      );
    case 'Activity':
      return (
        <p className="text-body-md">
          <Link href={`/overview#activity`} className="underline">
            the lifecycle timeline
          </Link>{' '}
          <span className="text-mute">— {name} v{version} appears there as it changes</span>
        </p>
      );
  }
}

function Fields({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="font-mono text-body-md">
      {rows.map(([label, value]) => (
        <div key={label} className="flex gap-3">
          <dt className="w-40 shrink-0 text-ash">{label}</dt>
          <dd className="text-ink break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

const VERDICT_TEXT: Record<string, string> = {
  eligible: 'can run here now',
  'not-currently-eligible':
    'not right now — a transient resource condition. Eviction, an idle release, or a job finishing resolves this.',
  incompatible:
    'structurally cannot run here. Waiting will not help and neither will eviction.',
  unknown: 'unknown — the agent is unreachable, so no compatibility claim is made.',
};

function Compatibility({ state }: { state: ReturnType<typeof useLive<RuntimeCompatibility>> }) {
  const c = state.data;
  return (
    <Panel title="Compatibility">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(state.ageMs)}
        {state.degraded.length > 0 && ` · unreachable: ${state.degraded.join(', ')}`}
      </p>
      {!c ? (
        <p className="text-body-md text-mute">unknown — no verdict could be computed</p>
      ) : (
        <>
          <p className="text-body-md text-ink">
            <span className="font-mono">{c.verdict}</span> — {VERDICT_TEXT[c.verdict]}
          </p>
          {c.reasons.length > 0 && (
            <ul className="mt-2 font-mono text-body-md text-mute">
              {c.reasons.map((reason) => (
                <li key={reason}>· {reason}</li>
              ))}
            </ul>
          )}
          {/* The two checks, side by side and never merged. Each shows its own reservation term,
              because invariant 1 counts every outstanding reservation against the budget while
              invariant 2 deducts only the not-yet-materialized ones from live free. */}
          <div className="mt-3 grid gap-4 font-mono text-body-md sm:grid-cols-2">
            <div>
              <p className="text-ash">budget check — {c.budgetCheck}</p>
              <Num label="estimated" value={c.estimatedVramGb} />
              <Num label="accounted resident" value={c.accountedResidentGb} />
              <Num label="reserved (all)" value={c.reservedGb} />
              <Num label="usable budget" value={c.usableBudgetGb} />
            </div>
            <div>
              <p className="text-ash">live-VRAM check — {c.liveVramCheck}</p>
              <Num label="estimated" value={c.estimatedVramGb} />
              <Num label="headroom" value={c.headroomGb} />
              <Num label="unmaterialized" value={c.unmaterializedGb} />
              <Num label="live free" value={c.liveFreeVramGb} />
            </div>
          </div>
          <p className="mt-3 font-mono text-caption-md text-ash">
            fits alone: {c.fitsAlone === null ? 'unknown' : c.fitsAlone ? 'yes' : 'no'} · engine:{' '}
            {c.requiredEngine ?? 'unknown'} · job exclusive: {c.jobExclusive ? 'yes' : 'no'}
          </p>
        </>
      )}
    </Panel>
  );
}

function Num({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <p>
      <span className="text-ash">{label}</span>{' '}
      {value === null || value === undefined ? (
        <span className="text-ash">unknown</span>
      ) : (
        <span className="text-ink">{value.toFixed(1)} GB</span>
      )}
    </p>
  );
}
