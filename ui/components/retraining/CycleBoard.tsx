'use client';

// 021 T442 (FR-246): the per-model cycle board — every policied model's last check, next due,
// pending (GPU-parked) retrain, and open suggestions, from GET /policies + /policies/:model/status.
// Carries the remaining policy CRUD verbs (pause/resume, delete, edit → loads the editor).

import { forwardRef, useCallback, useEffect, useImperativeHandle, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwDelete, gwGet, gwPut } from '@/lib/gw';
import type { PolicyDoc } from './PolicyEditor';

type Policy = PolicyDoc & { model_name: string };
type PolicyStatus = {
  policy: Policy;
  status: { last_check_at?: number; next_due_at?: number; results?: unknown[] };
  pending_retrain: { attempts: number; next_attempt_at: number } | null;
  open_suggestions: { id: string }[];
};

export type CycleBoardHandle = { refresh: () => void };

export const CycleBoard = forwardRef<
  CycleBoardHandle,
  { onEdit: (p: { model_name: string; doc: PolicyDoc }) => void }
>(function CycleBoard({ onEdit }, ref) {
  const [rows, setRows] = useState<PolicyStatus[] | null>(null);
  const [err, setErr] = useState('');

  const refresh = useCallback(async () => {
    try {
      const d = await gwGet<{ policies: Policy[] }>('policies');
      const statuses = await Promise.all(
        (d.policies || []).map((p) =>
          gwGet<PolicyStatus>(`policies/${encodeURIComponent(p.model_name)}/status`).catch(() => ({
            policy: p,
            status: {},
            pending_retrain: null,
            open_suggestions: [],
          })),
        ),
      );
      setRows(statuses);
      setErr('');
    } catch (e) {
      setErr(String(e));
      setRows((r) => r ?? []);
    }
  }, []);

  useImperativeHandle(ref, () => ({ refresh }), [refresh]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  const toggle = async (row: PolicyStatus) => {
    try {
      const { model_name, ...docFields } = row.policy;
      await gwPut(`policies/${encodeURIComponent(model_name)}`, {
        ...docFields,
        enabled: !row.policy.enabled,
      });
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const remove = async (name: string) => {
    try {
      await gwDelete(`policies/${encodeURIComponent(name)}`);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const fmt = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleTimeString() : 'never');

  return (
    <Panel title="cycle board" hint="GET /policies/{model}/status — last check · next due · pending retrain">
      {err && <p className="mb-2 text-caption-md st-danger">[x] {err}</p>}
      {rows === null ? (
        <p className="text-caption-md text-ash">[~] loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-body-md text-mute">
          [ ] no policies declared — the loop is manual until one exists.
        </p>
      ) : (
        <ul className="divide-y divide-hairline">
          {rows.map((row) => (
            <li key={row.policy.model_name} className="py-2 text-body-md">
              <div className="flex items-center justify-between gap-3">
                <span className="text-ink">
                  <span className={row.policy.enabled ? 'st-accent' : 'st-mute'}>
                    [{row.policy.enabled ? '●' : ' '}]
                  </span>{' '}
                  {row.policy.model_name}{' '}
                  <span className="text-mute">
                    · {row.policy.modality} · every {row.policy.check_interval_s}s ·{' '}
                    {row.policy.promotion_mode}
                    {row.policy.promotion_mode === 'auto-on-green' && (
                      <span className="st-warning"> [!]</span>
                    )}
                  </span>
                </span>
                <span className="flex gap-2 text-caption-md">
                  <button
                    onClick={() => {
                      const { model_name, ...docFields } = row.policy;
                      onEdit({ model_name, doc: docFields });
                    }}
                    className="underline text-mute"
                  >
                    edit
                  </button>
                  <button onClick={() => toggle(row)} className="underline text-mute">
                    {row.policy.enabled ? 'pause' : 'resume'}
                  </button>
                  <button
                    onClick={() => remove(row.policy.model_name)}
                    className="underline st-danger"
                  >
                    delete
                  </button>
                </span>
              </div>
              <div className="mt-1 text-caption-md text-mute">
                last check {fmt(row.status?.last_check_at)} · next due {fmt(row.status?.next_due_at)}
                {row.pending_retrain && (
                  <span className="st-warning">
                    {' '}
                    · [!] retrain parked (attempt {row.pending_retrain.attempts}, GPU busy — retries{' '}
                    {fmt(row.pending_retrain.next_attempt_at)})
                  </span>
                )}
                {row.open_suggestions.length > 0 && (
                  <span className="st-accent">
                    {' '}
                    · [→] {row.open_suggestions.length} open suggestion(s) — see the inbox below
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
});
