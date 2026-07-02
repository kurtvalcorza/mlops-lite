'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { GwError, gwDelete, gwDetail, gwGet, gwPost, gwPut } from '@/lib/gw';

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

// 018 US3 (FR-179): per-model policy documents + their scheduler status.
type Policy = {
  model_name: string;
  modality: string;
  monitors: { kind: string; [k: string]: unknown }[];
  check_interval_s: number;
  on_breach: { action: string; dataset: string; params: { dataset_name?: string } };
  promotion_mode: string;
  enabled: boolean;
};
type PolicyStatus = {
  policy: Policy;
  status: { last_check_at?: number; next_due_at?: number; results?: unknown[] };
  pending_retrain: { attempts: number; next_attempt_at: number } | null;
  open_suggestions: { id: string }[];
};

const MODALITIES = ['llm', 'vision', 'embeddings', 'asr'];
const MODES = ['manual', 'suggest', 'auto-on-green'];

export default function MonitorPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [refKey, setRefKey] = useState('');
  const [curKey, setCurKey] = useState('');
  const [threshold, setThreshold] = useState(0.25);
  const [withRetrain, setWithRetrain] = useState(false);
  const [outputName, setOutputName] = useState('drift-retrain');
  const [retrainModality, setRetrainModality] = useState('llm');

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
      // 018 (T369): the breach retrains the SELECTED modality (pre-018 this was hardcoded to an
      // LLM run); per-flow knobs come from each flow's defaults, like the Runs tab.
      const retrain: Record<string, unknown> = {
        dataset_name: cn,
        dataset_version: cv,
        output_name: outputName,
        modality: retrainModality,
      };
      if (retrainModality === 'llm') {
        retrain.steps = 10;
        retrain.lora_r = 8;
      }
      body.retrain = retrain;
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
            <>
              <Field label="retrain output name">
                <input
                  value={outputName}
                  onChange={(e) => setOutputName(e.target.value)}
                  className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
                />
              </Field>
              <Field label="retrain modality">
                <Select value={retrainModality} onChange={setRetrainModality} opts={MODALITIES} />
              </Field>
            </>
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
                  ) : res.retrain.skipped ? (
                    <p className="text-mute">[~] retrain skipped: {res.retrain.skipped}</p>
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

      <Policies datasetNames={datasets.map((d) => d.name)} />
    </>
  );
}

// --- 018 US3 (T371): declarative per-model policies — the loop, declared here -----------------------

function Policies({ datasetNames }: { datasetNames: string[] }) {
  const [rows, setRows] = useState<PolicyStatus[]>([]);
  const [err, setErr] = useState('');
  const [saving, setSaving] = useState(false);
  // editor state
  const [model, setModel] = useState('');
  const [modality, setModality] = useState('llm');
  const [monitorKind, setMonitorKind] = useState('quality');
  const [refKey, setRefKey] = useState('');
  const [intervalS, setIntervalS] = useState(900);
  const [datasetName, setDatasetName] = useState('');
  const [mode, setMode] = useState('manual');

  const refresh = async () => {
    try {
      const d = await gwGet<{ policies: Policy[] }>('policies');
      const statuses = await Promise.all(
        (d.policies || []).map((p) =>
          gwGet<PolicyStatus>(`policies/${encodeURIComponent(p.model_name)}/status`).catch(
            () => ({ policy: p, status: {}, pending_retrain: null, open_suggestions: [] }),
          ),
        ),
      );
      setRows(statuses);
    } catch (e) {
      setErr(String(e));
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    setSaving(true);
    setErr('');
    const monitor: Record<string, unknown> = { kind: monitorKind };
    if (monitorKind === 'input_drift' && refKey) {
      const [rn, rv] = refKey.split('@');
      monitor.reference = { name: rn, version: rv };
    }
    try {
      await gwPut(`policies/${encodeURIComponent(model.trim())}`, {
        modality,
        monitors: [monitor],
        check_interval_s: intervalS,
        on_breach: { action: 'retrain', dataset: 'latest', params: { dataset_name: datasetName } },
        promotion_mode: mode,
        enabled: true,
      });
      setModel('');
      await refresh();
    } catch (e) {
      // a validation 400 carries the structured {errors: [{field, reason}]} detail (FR-179)
      setErr(e instanceof GwError && e.status === 400 ? gwDetail(e) : String(e));
    } finally {
      setSaving(false);
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

  const toggle = async (row: PolicyStatus) => {
    try {
      const { model_name, ...doc } = row.policy;
      await gwPut(`policies/${encodeURIComponent(model_name)}`, {
        ...doc,
        enabled: !row.policy.enabled,
      });
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <div className="mt-6 grid gap-6 lg:grid-cols-[1fr_1.4fr]">
      <Panel title="declare policy" hint="PUT /policies/{model} — the loop, declared">
        <Field label="model name">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="e.g. vision-mobilenet"
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
          />
        </Field>
        <Field label="modality (the flow a breach retrains)">
          <Select value={modality} onChange={setModality} opts={MODALITIES} />
        </Field>
        <Field label="monitor">
          <Select value={monitorKind} onChange={setMonitorKind} opts={['quality', 'input_drift']} />
        </Field>
        {monitorKind === 'input_drift' && (
          <Field label="reference dataset @ version">
            <input
              value={refKey}
              onChange={(e) => setRefKey(e.target.value)}
              placeholder="name@version"
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            />
          </Field>
        )}
        <Field label="check interval (seconds, >= 60)">
          <input
            type="number"
            min={60}
            value={intervalS}
            onChange={(e) => setIntervalS(Number(e.target.value))}
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
          />
        </Field>
        <Field label="retrain dataset (latest version resolved at launch)">
          <Select value={datasetName} onChange={setDatasetName} opts={datasetNames} />
        </Field>
        <Field label="promotion mode (manual = today's behavior)">
          <Select value={mode} onChange={setMode} opts={MODES} />
        </Field>
        <button
          onClick={save}
          disabled={saving || !model.trim() || !datasetName}
          className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
        >
          {saving ? '[~] saving…' : '[+] save policy'}
        </button>
        {err && <p className="mt-3 whitespace-pre-wrap text-caption-md st-danger">[x] {err}</p>}
      </Panel>

      <Panel title="policies" hint="scheduled checks · pending retrains · suggestions">
        {rows.length === 0 && (
          <p className="text-body-md text-mute">[ ] no policies declared — the loop is manual.</p>
        )}
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
                  </span>
                </span>
                <span className="flex gap-2 text-caption-md">
                  <button onClick={() => toggle(row)} className="underline text-mute">
                    {row.policy.enabled ? 'pause' : 'resume'}
                  </button>
                  <button onClick={() => remove(row.policy.model_name)} className="underline st-danger">
                    delete
                  </button>
                </span>
              </div>
              <div className="mt-1 text-caption-md text-mute">
                last check{' '}
                {row.status?.last_check_at
                  ? new Date(row.status.last_check_at * 1000).toLocaleTimeString()
                  : 'never'}
                {row.pending_retrain && (
                  <span className="st-danger">
                    {' '}· [!] retrain parked (attempt {row.pending_retrain.attempts}, GPU busy)
                  </span>
                )}
                {row.open_suggestions.length > 0 && (
                  <span className="st-accent">
                    {' '}· [→] {row.open_suggestions.length} promotion suggestion(s) —{' '}
                    <Link href="/models" className="underline">
                      review in models
                    </Link>
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
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
