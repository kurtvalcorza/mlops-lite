'use client';

import { useEffect, useState } from 'react';
import { PageTitle } from '@/components/Panel';
import { NoRenderer, RENDERERS } from '@/components/serving';
import type { ActivationView, ServingState, TaskEntry } from '@/components/serving';
import { BatchPanel } from '@/components/serving/BatchPanel';
import { LeaseView } from '@/components/serving/LeaseView';
import { gwGet } from '@/lib/gw';

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
