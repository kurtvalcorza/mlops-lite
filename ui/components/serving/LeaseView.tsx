'use client';

// 021 T430 (FR-234): the full lease view — who holds the single GPU lease, what is resident, and
// every promoted engine with its tenancy marked: lease-tenant (llm / vision / asr — and training,
// which takes the same lease when a run is active) vs off-lease CPU (tabular / embed).
// Principle II visualize-only: reads serving/state + serving/tasks, never touches admission.

import { Panel } from '@/components/Panel';
import type { ServingState, TaskEntry } from './types';

// task tag → engine label + tenancy (the registry's task vocabulary, 009).
const TENANCY: Record<string, { engine: string; lease: boolean }> = {
  'text-generation': { engine: 'llm', lease: true },
  'image-classification': { engine: 'vision', lease: true },
  asr: { engine: 'asr', lease: true },
  embedding: { engine: 'embed', lease: false },
  tabular: { engine: 'predict', lease: false },
};

export function LeaseView({
  serving,
  tasks,
}: {
  serving: ServingState | null;
  tasks: TaskEntry[] | null;
}) {
  return (
    <Panel title="lease" hint="GET /serving/state + /serving/tasks — one model in VRAM (Principle II)">
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-body-md">
        <span className="text-mute">holder:</span>
        {serving === null ? (
          <span className="text-ash">unknown</span>
        ) : serving.holder ? (
          <span className="st-accent">[●] {serving.holder}</span>
        ) : (
          <span className="st-mute">[○] none (idle)</span>
        )}
        <span className="text-mute">resident:</span>
        {serving === null ? (
          <span className="text-ash">unknown</span>
        ) : serving.resident ? (
          <span className="text-ink">
            {serving.serving_model}
            {serving.serving_version ? `@v${serving.serving_version}` : ''}
          </span>
        ) : (
          <span className="text-ash">nothing loaded</span>
        )}
      </div>

      <p className="mb-1 text-caption-md text-mute">promoted engines</p>
      {tasks === null ? (
        <p className="text-caption-md text-ash">[~] discovering…</p>
      ) : tasks.length === 0 ? (
        <p className="text-caption-md text-ash">[ ] no serving models registered.</p>
      ) : (
        <ul className="space-y-1 text-caption-md">
          {tasks.map((t) => {
            const ten = t.task ? TENANCY[t.task] : undefined;
            const isHolder =
              !!serving?.holder && !!ten && ten.lease && serving.holder === ten.engine;
            return (
              <li key={`${t.model}@${t.version}`} className="flex items-baseline justify-between gap-3">
                <span className="text-ink">
                  <span className={isHolder ? 'st-accent' : 'st-mute'}>[{isHolder ? '●' : ' '}]</span>{' '}
                  {t.model}@v{t.version}
                  <span className="ml-2 text-ash">{t.task ?? '(no task tag)'}</span>
                </span>
                {ten ? (
                  ten.lease ? (
                    <span className="st-warning">lease-tenant</span>
                  ) : (
                    <span className="text-mute">off-lease (CPU)</span>
                  )
                ) : (
                  <span className="text-ash">unknown tenancy</span>
                )}
              </li>
            );
          })}
          {/* training is a lease tenant too — it has no serving task row, so state it explicitly */}
          <li className="flex items-baseline justify-between gap-3 text-ash">
            <span>
              <span className={serving?.holder === 'training' ? 'st-accent' : 'st-mute'}>
                [{serving?.holder === 'training' ? '●' : ' '}]
              </span>{' '}
              training runs (when active)
            </span>
            <span className="st-warning">lease-tenant</span>
          </li>
        </ul>
      )}
      <p className="mt-3 text-caption-md text-ash">
        [i] lease tenants hold the one GPU slot sequentially; off-lease engines answer on CPU at any
        time. A running training/HPO/batch job is never preempted.
      </p>
    </Panel>
  );
}
