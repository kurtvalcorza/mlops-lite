'use client';

// 021 T435 (FR-238/240/241/242): the manual input-drift check (PSI, reference vs current dataset
// version) with the one-shot retrain arm. Adapted from the pre-021 monitor page; the standing
// policy loop lives in the retraining stage.

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
type Report = {
  features: Record<string, number>;
  drifted_columns: string[];
  max_psi: number;
  dataset_drift: boolean;
  threshold: number;
};
type CheckResp = {
  report: Report;
  retrain: { run_id?: string; error?: string; skipped?: string } | null;
};

export function DriftPanel({ onRan }: { onRan?: () => void }) {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [refKey, setRefKey] = useState('');
  const [curKey, setCurKey] = useState('');
  const [threshold, setThreshold] = useState(0.25);
  const [armed, setArmed] = useState(false);
  const [draft, setDraft] = useState<RetrainDraft>({
    dataset_name: '',
    output_name: 'drift-retrain',
    modality: 'llm',
  });
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [res, setRes] = useState<CheckResp | null>(null);

  useEffect(() => {
    gwGet<{ datasets: Dataset[] }>('datasets')
      .then((d) => {
        setDatasets(d.datasets || []);
        const opts = (d.datasets || []).flatMap((x) =>
          x.versions.map((v) => `${x.name}@${v.version}`),
        );
        if (opts[0]) setRefKey(opts[0]);
        if (opts[1]) setCurKey(opts[1]);
        else if (opts[0]) setCurKey(opts[0]);
      })
      .catch(() => setDatasets([]));
  }, []);

  const opts = datasets.flatMap((d) => d.versions.map((v) => `${d.name}@${v.version}`));

  const run = async () => {
    if (!refKey || !curKey) return;
    setBusy(true);
    setErr('');
    setRes(null);
    const [rn, rv] = refKey.split('@');
    const [cn, cv] = curKey.split('@');
    const body: Record<string, unknown> = {
      reference: { name: rn, version: rv },
      current: { name: cn, version: cv },
      threshold,
    };
    if (armed && draft.dataset_name) {
      // auto-fill the current dataset when the operator armed but left the field on its default
      body.retrain = buildRetrainSpec(draft);
    }
    try {
      const r = await gwPost<CheckResp>('monitor/check', body);
      setRes(r);
      onRan?.();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  // Arming a one-shot retrain requires an explicit confirmation naming what will happen (T438).
  const submit = () => {
    if (armed && draft.dataset_name) setConfirming(true);
    else run();
  };

  const report = res?.report;
  const feats = report ? Object.entries(report.features) : [];

  return (
    <Panel title="drift check (manual, one-shot)" hint="POST /monitor/check — reference vs current PSI">
      <Field label="reference @ version">
        <Select value={refKey} onChange={setRefKey} opts={opts} />
      </Field>
      <Field label="current @ version">
        <Select value={curKey} onChange={setCurKey} opts={opts} />
      </Field>
      <Field label="threshold (PSI)">
        <input
          type="number"
          step="0.05"
          min={0}
          value={threshold}
          onChange={(e) => setThreshold(Number(e.target.value))}
          className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
        />
      </Field>

      <OneShotRetrain
        datasetNames={datasets.map((d) => d.name)}
        armed={armed}
        onArm={(a) => {
          setArmed(a);
          // prefill from the check when arming: retrain on the *current* (drifted) dataset
          if (a && !draft.dataset_name && curKey) {
            setDraft({ ...draft, dataset_name: curKey.split('@')[0] });
          }
        }}
        draft={draft}
        onChange={setDraft}
      />

      <button
        onClick={submit}
        disabled={busy || !refKey || !curKey || (armed && (!draft.dataset_name || !draft.output_name.trim()))}
        className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        {busy ? '[~] checking…' : armed ? '[!] run check (retrain armed)' : '[+] run check'}
      </button>
      {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}

      {report && (
        <div className="mt-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-body-strong text-ink">
              max PSI {report.max_psi} · threshold {report.threshold}
            </span>
            <Badge tone={report.dataset_drift ? 'danger' : 'success'}>
              {report.dataset_drift ? 'drift detected' : 'no drift'}
            </Badge>
          </div>
          <ul className="divide-y divide-hairline">
            {feats.map(([f, psi]) => {
              const drifted = report.drifted_columns.includes(f);
              return (
                <li key={f} className="flex items-baseline justify-between gap-3 py-1 text-body-md">
                  <span className="text-ink">
                    <span className={drifted ? 'st-danger' : 'st-mute'}>[{drifted ? '!' : ' '}]</span>{' '}
                    {f}
                  </span>
                  <span className={drifted ? 'st-danger' : 'text-mute'}>{psi}</span>
                </li>
              );
            })}
          </ul>
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

function Select({
  value,
  onChange,
  opts,
}: {
  value: string;
  onChange: (s: string) => void;
  opts: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
    >
      {opts.length === 0 && <option value="">(no datasets)</option>}
      {opts.map((o) => (
        <option key={o} value={o}>
          {o.replace('@', ' @ ')}
        </option>
      ))}
    </select>
  );
}
