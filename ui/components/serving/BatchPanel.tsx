'use client';

// 021 T431 (FR-236): offline batch inference lives in the SERVING stage (it scores a pinned
// dataset version through the serving path) — moved here from the runs/training page, which now
// launches training only. Launch over dataset@version → poll batch/:id → result link.

import { useEffect, useRef, useState } from 'react';
import { Badge } from '@/components/Badge';
import { Panel } from '@/components/Panel';
import { GwError, gwGet, gwPost } from '@/lib/gw';

type Dataset = { name: string; versions: { version: string }[] };
type BatchRec = {
  batch_id?: string;
  status?: string;
  result?: { n_in: number; n_out: number; n_failed: number; result_uri: string } | null;
  error?: string | null;
};

// 014 — the trainer emits batch terminal status as 'succeeded'/'failed' (not 'completed').
const BATCH_TERMINAL = new Set(['succeeded', 'failed']);
// 014 — batch has its OWN modality set: the /batch validator accepts only these (LLM, vision,
// tabular); the training modality list (embeddings/asr) would 400 here.
const BATCH_MODALITIES = ['llm', 'vision', 'tabular'] as const;
type BatchModality = (typeof BATCH_MODALITIES)[number];

export function BatchPanel() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [dsKey, setDsKey] = useState('');
  const [model, setModel] = useState('');
  const [modality, setModality] = useState<BatchModality>('llm');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [rec, setRec] = useState<BatchRec | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    gwGet<{ datasets: Dataset[] }>('datasets')
      .then((d) => setDatasets(d.datasets || []))
      .catch(() => setDatasets([]));
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const opts = datasets.flatMap((d) =>
    d.versions.map((v) => ({ key: `${d.name}@${v.version}`, label: `${d.name} @ ${v.version}` })),
  );

  const watch = (id: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    const tick = async () => {
      try {
        const b = await gwGet<BatchRec>(`batch/${encodeURIComponent(id)}`);
        setRec(b);
        if (b.status && BATCH_TERMINAL.has(b.status) && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        /* transient — keep polling */
      }
    };
    tick();
    pollRef.current = setInterval(tick, 4000);
  };

  const launch = async () => {
    if (!dsKey || !model.trim()) return;
    const [dataset_name, dataset_version] = dsKey.split('@');
    setBusy(true);
    setErr('');
    setRec(null);
    try {
      const res = await gwPost<{ batch_id: string; status: string }>('batch', {
        dataset_name,
        dataset_version,
        model: model.trim(),
        modality,
      });
      setRec({ batch_id: res.batch_id, status: res.status });
      watch(res.batch_id);
    } catch (e) {
      setErr(
        e instanceof GwError && e.status === 409
          ? 'Refused: the daemon is busy (a run/study/batch is active).'
          : String(e),
      );
    } finally {
      setBusy(false);
    }
  };

  const running = rec?.status && !BATCH_TERMINAL.has(rec.status);

  return (
    <Panel title="batch inference" hint="POST /batch — score a dataset version offline">
      <div className="grid gap-2 sm:grid-cols-3">
        <Field label="dataset @ version">
          <select
            value={dsKey}
            onChange={(e) => setDsKey(e.target.value)}
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
          >
            <option value="">(pick a version)</option>
            {opts.map((o) => (
              <option key={o.key} value={o.key}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="model (alias or version)">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="@serving"
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
          />
        </Field>
        <Field label="modality">
          <select
            value={modality}
            onChange={(e) => setModality(e.target.value as BatchModality)}
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
          >
            {BATCH_MODALITIES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <button
        onClick={launch}
        disabled={busy || !dsKey || !model.trim() || !!running}
        className="mt-2 rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        {busy ? '[~] launching…' : '[+] score dataset'}
      </button>
      {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}
      {rec && (
        <div className="mt-3 text-caption-md">
          <Badge
            tone={rec.status === 'succeeded' ? 'success' : rec.status === 'failed' ? 'danger' : 'accent'}
          >
            {rec.status}
          </Badge>
          {rec.error && <p className="mt-1 st-danger">[x] {rec.error}</p>}
          {rec.result && (
            <p className="mt-1 st-success">
              [✓] {rec.result.n_out}/{rec.result.n_in} scored
              {rec.result.n_failed > 0 ? ` · ${rec.result.n_failed} failed` : ''} ·{' '}
              <span className="text-ash">{rec.result.result_uri}</span>
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <label className="mb-1 block text-caption-md text-mute">{label}</label>
      {children}
    </div>
  );
}
