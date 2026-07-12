'use client';

// 021 T430 (FR-234): the full lease view — who holds the single GPU lease, what is resident, and
// every promoted engine with its tenancy marked: lease-tenant (llm / vision / asr — and training,
// which takes the same lease when a run is active) vs off-lease CPU (tabular / embed).
// Principle II visualize-only: reads serving/state + serving/tasks, never touches admission.

import { Panel } from '@/components/Panel';
import type { ActivationView, ServingState, TaskEntry } from './types';

// task tag → engine label + tenancy (the registry's task vocabulary, 009).
const TENANCY: Record<string, { engine: string; lease: boolean }> = {
  'text-generation': { engine: 'llm', lease: true },
  'image-classification': { engine: 'vision', lease: true },
  asr: { engine: 'asr', lease: true },
  embedding: { engine: 'embed', lease: false },
  tabular: { engine: 'predict', lease: false },
};

// 023 US5 (T525): desired vs resident, honestly. `desired` is the pointer the operator promoted;
// `resident` is what the AGENT reports actually loaded. An incomplete activation is shown by its
// state (reloading/degraded/…) with retry guidance — never labeled as serving (FR-311).
function ActivationLine({ activation }: { activation: ActivationView | null }) {
  if (!activation) return null;
  const { desired, resident, activation: op, consistent } = activation;
  if (!desired.model_name && !op) return null; // nothing selected yet — the resident line suffices
  return (
    <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption-md">
      <span className="text-mute">desired:</span>
      <span className="text-ink">{desired.model_name ?? '(default base)'}</span>
      <span className="text-mute">resident:</span>
      <span className="text-ink">
        {resident.model_name ?? 'unknown'}
        {resident.version ? `@v${resident.version}` : ''}
      </span>
      {consistent ? (
        <span className="st-accent">[●] consistent</span>
      ) : op ? (
        <span className={op.state === 'degraded' ? 'st-error' : 'st-warning'}>
          [{op.state === 'degraded' ? '!' : '~'}] activation {op.state}
          {op.last_error ? ` — ${op.last_error}` : ''}
          {op.state === 'degraded' || op.state === 'reloading'
            ? ' (re-promote the target to retry, or the reconciler converges it)'
            : ''}
        </span>
      ) : (
        <span className="st-warning">[~] desired ≠ resident (converging on next load)</span>
      )}
    </div>
  );
}

export function LeaseView({
  serving,
  tasks,
  activation = null,
}: {
  serving: ServingState | null;
  tasks: TaskEntry[] | null;
  activation?: ActivationView | null;
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

      <ActivationLine activation={activation} />

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
