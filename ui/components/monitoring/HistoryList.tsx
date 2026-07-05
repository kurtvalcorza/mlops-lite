'use client';

// 021 T436 (FR-238): the monitoring READ side — both report histories, newest first. Pre-021 the
// checks wrote reports the console never showed; this closes that gap. Exposes a refresh handle so
// a just-run check appears without waiting for the next poll.

import { forwardRef, useCallback, useEffect, useImperativeHandle, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwGet } from '@/lib/gw';

type DriftReport = {
  report_id?: string;
  reference: string;
  current: string;
  threshold: number;
  max_psi: number;
  drifted_columns: string[];
  dataset_drift: boolean;
  created_at: number;
};
type QualityReport = {
  report_id?: string;
  model_name: string | null;
  model_version: string;
  modality: string;
  metric: string | null;
  value: number | null;
  baseline: number | null;
  insufficient_data: boolean;
  breach: boolean;
  created_at: number;
};

export type HistoryHandle = { refresh: () => void };

export const HistoryList = forwardRef<HistoryHandle>(function HistoryList(_props, ref) {
  const [drift, setDrift] = useState<DriftReport[] | null>(null);
  const [qual, setQual] = useState<QualityReport[] | null>(null);
  const [err, setErr] = useState('');

  const load = useCallback(async () => {
    const [d, q] = await Promise.all([
      gwGet<{ reports: DriftReport[] }>('monitor?limit=10').catch((e) => {
        setErr(String(e));
        return null;
      }),
      gwGet<{ reports: QualityReport[] }>('monitor/quality?limit=10').catch(() => null),
    ]);
    if (d) {
      setDrift(d.reports ?? []);
      setErr('');
    }
    if (q) setQual(q.reports ?? []);
  }, []);

  useImperativeHandle(ref, () => ({ refresh: load }), [load]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Panel title="drift history" hint="GET /monitor — newest first">
        {err && <p className="mb-2 text-caption-md st-danger">[x] {err}</p>}
        {drift === null ? (
          <p className="text-caption-md text-ash">[~] loading…</p>
        ) : drift.length === 0 ? (
          <p className="text-body-md text-mute">[ ] no drift checks recorded yet.</p>
        ) : (
          <ul className="divide-y divide-hairline">
            {drift.map((r, i) => (
              <li key={r.report_id ?? i} className="py-1.5 text-caption-md">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-ink">
                    <span className={r.dataset_drift ? 'st-danger' : 'st-success'}>
                      [{r.dataset_drift ? '!' : '✓'}]
                    </span>{' '}
                    {r.reference} → {r.current}
                  </span>
                  <span className={r.dataset_drift ? 'st-danger' : 'text-mute'}>
                    PSI {r.max_psi} / {r.threshold}
                  </span>
                </div>
                <p className="text-ash">
                  {new Date(r.created_at * 1000).toLocaleString()}
                  {r.drifted_columns.length > 0 && <> · drifted: {r.drifted_columns.join(', ')}</>}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="quality history" hint="GET /monitor/quality — newest first">
        {qual === null ? (
          <p className="text-caption-md text-ash">[~] loading…</p>
        ) : qual.length === 0 ? (
          <p className="text-body-md text-mute">[ ] no quality checks recorded yet.</p>
        ) : (
          <ul className="divide-y divide-hairline">
            {qual.map((r, i) => (
              <li key={r.report_id ?? i} className="py-1.5 text-caption-md">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-ink">
                    <span
                      className={
                        r.insufficient_data ? 'st-warning' : r.breach ? 'st-danger' : 'st-success'
                      }
                    >
                      [{r.insufficient_data ? '?' : r.breach ? '!' : '✓'}]
                    </span>{' '}
                    {r.model_name ?? '?'} v{r.model_version} · {r.modality}
                  </span>
                  <span className={r.breach ? 'st-danger' : 'text-mute'}>
                    {r.metric ?? '—'}={r.value != null ? r.value : '·'}
                    {r.baseline != null ? ` / base ${r.baseline}` : ''}
                  </span>
                </div>
                <p className="text-ash">{new Date(r.created_at * 1000).toLocaleString()}</p>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
});
