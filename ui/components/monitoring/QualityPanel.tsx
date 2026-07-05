'use client';

// 021 T435 (FR-238/241): the manual output-quality check — windowed metric for a served version vs
// its 011 eval baseline. The baseline AUTO-RESOLVES (the registry's logged eval metric); an
// advanced disclosure exposes baseline / window_n / drop_pct for the operator who wants to steer
// (FR-241). Same one-shot retrain arm + cooldown-as-outcome as the drift panel.

import { useEffect, useState } from 'react';
import { Badge } from '@/components/Badge';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';
import {
  buildRetrainSpec,
  OneShotRetrain,
  RetrainOutcome,
  type RetrainDraft,
} from './OneShotRetrain';

type Dataset = { name: string; versions: { version: string }[] };
type QualityReport = {
  model_name: string | null;
  model_version: string;
  modality: string;
  metric: string | null;
  value: number | null;
  direction: string | null;
  n_labeled: number;
  n_window: number;
  window_n: number;
  baseline: number | null;
  drop_pct: number;
  insufficient_data: boolean;
  breach: boolean;
};
type CheckResp = {
  report: QualityReport;
  retrain: { run_id?: string; error?: string; skipped?: string } | null;
};

// The quality scorer's modality vocabulary = the registry task tags (011 metric registry).
const QUALITY_MODALITIES = ['text-generation', 'image-classification', 'tabular', 'embedding', 'asr'];

export function QualityPanel({ onRan }: { onRan?: () => void }) {
  const [modelName, setModelName] = useState('');
  const [modelVersion, setModelVersion] = useState('');
  const [modality, setModality] = useState('text-generation');
  const [advanced, setAdvanced] = useState(false);
  const [baseline, setBaseline] = useState(''); // blank = auto-resolve from the 011 eval baseline
  const [windowN, setWindowN] = useState(50);
  const [dropPct, setDropPct] = useState(10);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [armed, setArmed] = useState(false);
  const [draft, setDraft] = useState<RetrainDraft>({
    dataset_name: '',
    output_name: 'quality-retrain',
    modality: 'llm',
  });
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [res, setRes] = useState<CheckResp | null>(null);

  useEffect(() => {
    gwGet<{ datasets: Dataset[] }>('datasets')
      .then((d) => setDatasets(d.datasets || []))
      .catch(() => setDatasets([]));
  }, []);

  const run = async () => {
    if (!modelVersion.trim()) return;
    setBusy(true);
    setErr('');
    setRes(null);
    const body: Record<string, unknown> = {
      model_version: modelVersion.trim(),
      modality,
      window_n: windowN,
      drop_pct: dropPct,
    };
    if (modelName.trim()) body.model_name = modelName.trim();
    if (advanced && baseline.trim() !== '') body.baseline = Number(baseline);
    if (armed && draft.dataset_name) body.retrain = buildRetrainSpec(draft);
    try {
      const r = await gwPost<CheckResp>('monitor/quality/check', body);
      setRes(r);
      onRan?.();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = () => {
    if (armed && draft.dataset_name) setConfirming(true);
    else run();
  };

  const report = res?.report;

  return (
    <Panel
      title="quality check (manual, one-shot)"
      hint="POST /monitor/quality/check — windowed metric vs the 011 baseline"
    >
      <div className="grid grid-cols-2 gap-2">
        <Field label="model name (baseline lookup)">
          <input
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="e.g. vision-mobilenet"
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
          />
        </Field>
        <Field label="model version">
          <input
            value={modelVersion}
            onChange={(e) => setModelVersion(e.target.value)}
            placeholder="e.g. 3"
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
          />
        </Field>
      </div>
      <Field label="modality (metric registry key)">
        <select
          value={modality}
          onChange={(e) => setModality(e.target.value)}
          className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
        >
          {QUALITY_MODALITIES.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </Field>

      {/* baseline auto-resolves; the advanced disclosure is for steering, not the common case */}
      <button
        onClick={() => setAdvanced(!advanced)}
        className="mb-2 text-caption-md text-mute underline"
      >
        [{advanced ? '−' : '+'}] advanced (baseline / window / drop%)
      </button>
      {advanced && (
        <div className="mb-2 grid grid-cols-3 gap-2">
          <Field label="baseline (blank = auto)">
            <input
              value={baseline}
              onChange={(e) => setBaseline(e.target.value)}
              placeholder="auto (011 eval)"
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
            />
          </Field>
          <Field label="window_n">
            <input
              type="number"
              min={1}
              value={windowN}
              onChange={(e) => setWindowN(Number(e.target.value))}
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            />
          </Field>
          <Field label="drop_pct">
            <input
              type="number"
              min={0}
              value={dropPct}
              onChange={(e) => setDropPct(Number(e.target.value))}
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            />
          </Field>
        </div>
      )}

      <OneShotRetrain
        datasetNames={datasets.map((d) => d.name)}
        armed={armed}
        onArm={setArmed}
        draft={draft}
        onChange={setDraft}
      />

      <button
        onClick={submit}
        disabled={
          busy || !modelVersion.trim() || (armed && (!draft.dataset_name || !draft.output_name.trim()))
        }
        className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        {busy ? '[~] checking…' : armed ? '[!] run check (retrain armed)' : '[+] run check'}
      </button>
      {err && <p className="mt-3 whitespace-pre-wrap text-caption-md st-danger">[x] {err}</p>}

      {report && (
        <div className="mt-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-body-strong text-ink">
              {report.model_name ?? '?'} v{report.model_version} · {report.modality}
            </span>
            <Badge
              tone={report.insufficient_data ? 'warning' : report.breach ? 'danger' : 'success'}
            >
              {report.insufficient_data ? 'insufficient data' : report.breach ? 'breach' : 'healthy'}
            </Badge>
          </div>
          <dl className="space-y-1 text-caption-md">
            <Row k="windowed metric">
              {report.metric ?? '—'} ={' '}
              {report.value != null ? report.value : '(not enough labeled pairs)'}
              {report.direction ? ` (${report.direction} is better)` : ''}
            </Row>
            <Row k="baseline">
              {report.baseline != null ? `${report.baseline} · breach below ${report.drop_pct}% drop` : '—'}
            </Row>
            <Row k="window">
              {report.n_window}/{report.window_n} scored · {report.n_labeled} labeled pairs
            </Row>
          </dl>
          <div className="mt-2">
            <RetrainOutcome retrain={res?.retrain} />
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirming}
        title="one-shot retrain armed"
        body={
          <>
            If this check breaches, a retrain launches immediately:{' '}
            <span className="text-ink">
              {draft.modality} on {draft.dataset_name}@latest → {draft.output_name}
            </span>{' '}
            (knobs defaulted, shared cooldown applies). One shot — this check only.
          </>
        }
        confirmLabel="run check + arm retrain"
        onConfirm={() => {
          setConfirming(false);
          run();
        }}
        onCancel={() => setConfirming(false)}
      />
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

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-mute">{k}</dt>
      <dd className="text-ink">{children}</dd>
    </div>
  );
}
