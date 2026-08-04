'use client';

import { useEffect, useState } from 'react';
import { PageTitle, Panel } from '@/components/Panel';
import { NoRenderer, RENDERERS } from '@/components/serving';
import type { ActivationView, ServingState, TaskEntry } from '@/components/serving';
import { BatchPanel } from '@/components/serving/BatchPanel';
import { LeaseView } from '@/components/serving/LeaseView';
import { gwGet } from '@/lib/gw';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { PlatformEndpoint } from '@/lib/platform-types';

/** Poll the gateway's GPU/lease state so the stage reflects what is actually resident (008 US3). */
function useServingState(intervalMs = 4000): ServingState | null {
  const [state, setState] = useState<ServingState | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<ServingState>('serving/state')
        .then((s) => alive && setState(s))
        .catch(() => alive && setState(null)); // unknown, not stale — the lease view says so
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return state;
}

/** Discover the registry's serving tasks → one panel per task (009 US1, FR-077/FR-231). Polled so a
 *  newly seeded modality appears without a reload. `null` until the first fetch resolves. */
function useTasks(intervalMs = 8000): TaskEntry[] | null {
  const [tasks, setTasks] = useState<TaskEntry[] | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<{ tasks: TaskEntry[] }>('serving/tasks')
        .then((d) => alive && setTasks(d.tasks ?? []))
        .catch(() => alive && setTasks([]));
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return tasks;
}

/** 023 US5 (T525): the desired/resident/activation read model — polled so a promote's activation
 *  progress (reloading -> active, or degraded with its error) shows without a reload. */
function useActivation(intervalMs = 5000): ActivationView | null {
  const [view, setView] = useState<ActivationView | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<ActivationView>('serving/llm/activation')
        .then((v) => alive && setView(v))
        .catch(() => alive && setView(null)); // pre-023 gateway or outage — the line hides itself
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return view;
}

// 021 T429 (FR-231..236): the serving stage — every promoted engine as a live panel under ONE GPU
// lease (LeaseView), plus offline batch (moved here from runs). The default landing surface.
export default function ServingPage() {
  const serving = useServingState();
  const tasks = useTasks();
  const activation = useActivation();

  return (
    <>
      <PageTitle sub="Every promoted engine, live, under one GPU lease. The API key stays server-side (BFF).">
        serving
      </PageTitle>

      <div className="mb-6">
        <LeaseView serving={serving} tasks={tasks} activation={activation} />
      </div>

      {/* 027 US7 (T750): the read model — desired and resident, never conflated. */}
      <div className="mb-6">
        <EndpointTable />
      </div>

      {tasks === null ? (
        <p className="text-caption-md text-ash">[~] discovering tasks…</p>
      ) : tasks.length === 0 ? (
        <p className="text-caption-md text-ash">
          [i] no serving models registered — seed a model (e.g. scripts/reseed_registry.sh) to
          render a panel.
        </p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
          {tasks.map((entry) => {
            const Renderer = (entry.task && RENDERERS[entry.task]) || NoRenderer;
            return <Renderer key={`${entry.model}@${entry.version}`} entry={entry} serving={serving} />;
          })}
        </div>
      )}

      <div className="mt-6">
        <BatchPanel />
      </div>
    </>
  );
}

/**
 * 027 US7 (T750) — the endpoint read model.
 *
 * **Desired and resident are separate columns and are never merged.** An in-progress activation is
 * exactly the case where they legitimately differ, and it is exactly when an operator is looking; a
 * single "model" column would show one of the two and quietly imply it was both.
 *
 * `stopped` here does not mean broken. A GPU modality that is not resident because a job holds the
 * GPU is stopped by design — on-demand loading is Principle II, and labelling it a failure would
 * send an operator to debug a system that is working exactly as intended. The legend below says so
 * on the surface rather than relying on the reader to know.
 *
 * **No rollout control is rendered** (FR-418). The gateway implements no traffic splitting, and a
 * decorative slider that silently does nothing is worse than an absent one — availability comes
 * from `console/capabilities`, so an unsupported control is absent rather than inert.
 */
function EndpointTable() {
  const { data, ageMs, degraded } = useLive<PlatformEndpoint[]>(
    'console/endpoints',
    CADENCE.runtime,
  );

  return (
    <Panel title="Endpoints">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>
      {data === null ? (
        <p className="text-body-md text-mute">unknown — neither the registry nor the agent answered</p>
      ) : data.length === 0 ? (
        <p className="text-body-md text-mute">nothing promoted to serving</p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full font-mono text-body-md">
              <thead className="text-ash">
                <tr>
                  <th className="pr-4 text-left">modality</th>
                  <th className="pr-4 text-left">status</th>
                  <th className="pr-4 text-left">desired</th>
                  <th className="pr-4 text-left">resident</th>
                  <th className="text-left">engine</th>
                </tr>
              </thead>
              <tbody>
                {data.map((endpoint) => (
                  <tr key={endpoint.id} className={endpoint.conflict ? 'text-ink' : 'text-mute'}>
                    <td className="pr-4">{endpoint.modality}</td>
                    <td className="pr-4 text-ink">{endpoint.status}</td>
                    <td className="pr-4">
                      {endpoint.desired.modelName ?? '—'}
                      {endpoint.desired.version && ` v${endpoint.desired.version}`}
                    </td>
                    {/* The agent-reported LOADED identity, never the desired pointer. */}
                    <td className="pr-4">
                      {endpoint.resident.modelIdentity ?? (
                        <span className="text-ash">not resident</span>
                      )}
                    </td>
                    <td>{endpoint.resident.engineId ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-caption-md text-ash">
            [i] <span className="text-mute">stopped</span> is not broken: a GPU modality loads on
            demand, so it is unloaded whenever a job or another model holds the GPU.{' '}
            <span className="text-mute">healthy</span> requires the model to be confirmed resident,
            never just promoted.
          </p>
        </>
      )}
    </Panel>
  );
}
