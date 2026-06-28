'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';

type Dataset = { name: string; versions: { version: string }[] };
type Report = {
  features: Record<string, number>;
  drifted_columns: string[];
  max_psi: number;
  dataset_drift: boolean;
  threshold: number;
};
type CheckResp = { report: Report; retrain: { run_id?: string; error?: string } | null };

export default function MonitorPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [refKey, setRefKey] = useState('');
  const [curKey, setCurKey] = useState('');
  const [threshold, setThreshold] = useState(0.25);
  const [withRetrain, setWithRetrain] = useState(false);
  const [outputName, setOutputName] = useState('drift-retrain');

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
    if (withRetrain) {
      body.retrain = {
        dataset_name: cn,
        dataset_version: cv,
        output_name: outputName,
        steps: 10,
        lora_r: 8,
      };
    }
    try {
      setRes(await gwPost<CheckResp>('monitor/check', body));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const report = res?.report;
  const feats = report ? Object.entries(report.features) : [];

  return (
    <>
      <PageTitle sub="Compare a reference vs current dataset version (PSI); optionally trigger retrain.">
        monitor
      </PageTitle>

      <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
        <Panel title="drift check" hint="POST /monitor/check">
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
          <label className="mb-3 flex items-center gap-2 text-caption-md text-mute">
            <input
              type="checkbox"
              checked={withRetrain}
              onChange={(e) => setWithRetrain(e.target.checked)}
            />
            launch retrain on breach
          </label>
          {withRetrain && (
            <Field label="retrain output name">
              <input
                value={outputName}
                onChange={(e) => setOutputName(e.target.value)}
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
              />
            </Field>
          )}
          <button
            onClick={run}
            disabled={busy || !refKey || !curKey}
            className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
          >
            {busy ? '[~] checking…' : '[+] run check'}
          </button>
          {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}
        </Panel>

        <Panel title="report" hint="per-feature PSI">
          {!report && <p className="text-body-md text-mute">[ ] no check run yet.</p>}
          {report && (
            <>
              <div className="mb-3 flex items-center justify-between">
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
                    <li key={f} className="flex items-baseline justify-between gap-3 py-1.5 text-body-md">
                      <span className="text-ink">
                        <span className={drifted ? 'st-danger' : 'st-mute'}>
                          [{drifted ? '!' : ' '}]
                        </span>{' '}
                        {f}
                      </span>
                      <span className={drifted ? 'st-danger' : 'text-mute'}>{psi}</span>
                    </li>
                  );
                })}
              </ul>

              {res?.retrain && (
                <div className="mt-3 text-caption-md">
                  {res.retrain.error ? (
                    <p className="st-danger">[x] retrain failed: {res.retrain.error}</p>
                  ) : (
                    <p className="st-accent">
                      [→] retrain launched: {res.retrain.run_id} —{' '}
                      <Link href="/runs" className="underline">
                        watch in runs
                      </Link>
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </Panel>
      </div>
    </>
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
