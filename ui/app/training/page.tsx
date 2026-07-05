'use client';

import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { GwError, gwGet, gwPost } from '@/lib/gw';

type DsVersion = { version: string };
type Dataset = { name: string; versions: DsVersion[] };
type RunRec = {
  run_id?: string;
  status?: string;
  model?: { name: string; version: string } | null;
  metrics?: Record<string, unknown> | null;
  error?: string | null;
};
type ServingState = {
  holder: string | null;
  resident: boolean;
  serving_model: string;
  serving_version: string | null;
};

const TERMINAL = new Set(['completed', 'failed']);

// 012 — an HPO study: a best trial (winning params + eval metric → a registered, promotable version).
type StudyBest = {
  version: string;
  value: number;
  metric: string | null;
  params: Record<string, unknown>;
} | null;
type StudyRec = {
  study_id?: string;
  status?: string;
  best?: StudyBest;
  summary?: { completed?: number; n_trials?: number } | null;
  error?: string | null;
};

// 010/021 T451 (FR-219/220): the FIXED 4-way modality picker — each modality shows only ITS knobs
// + its pinned default base; the rest fall to the flow's conservative VRAM-fitting defaults.
const MODALITIES = ['llm', 'vision', 'embeddings', 'asr'] as const;
type Modality = (typeof MODALITIES)[number];
// Only these modalities can resume from a prior registered version (their artifact reloads as a
// trainable warm start); LLM/ASR register a serving GGUF/ggml, not a trainable checkpoint.
const CHAINABLE = new Set<Modality>(['vision', 'embeddings']);
// Vision's architecture is locked (the classifier head is sized to the arch) — base is read-only.
const LOCKED_BASE = new Set<Modality>(['vision']);

// 021 T451: batch inference MOVED to /serving (it scores through the serving path, FR-236) — this
// page launches training only; the old BatchLauncher is gone on purpose (no duplication).
export default function TrainingPage() {
  return (
    <Suspense fallback={<p className="text-caption-md text-ash">[~] loading…</p>}>
      <TrainingView />
    </Suspense>
  );
}

function TrainingView() {
  const params = useSearchParams();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [dsKey, setDsKey] = useState(''); // "name@version"
  const [outputName, setOutputName] = useState('');
  const [modality, setModality] = useState<Modality>('llm');
  const [baseModel, setBaseModel] = useState('');
  const [parentVersion, setParentVersion] = useState('');
  const [steps, setSteps] = useState(10);
  const [loraR, setLoraR] = useState(8);
  const [epochs, setEpochs] = useState(3);
  const [seed, setSeed] = useState(0);

  // 012 — HPO: an "optimize" toggle turns the launch into a study of N sequential trials.
  const [optimize, setOptimize] = useState(false);
  const [nTrials, setNTrials] = useState(15);
  const [study, setStudy] = useState<StudyRec | null>(null);

  const [launching, setLaunching] = useState(false);
  const [refusal, setRefusal] = useState('');
  const [err, setErr] = useState('');
  const [rec, setRec] = useState<RunRec | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [lease, setLease] = useState<ServingState | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const runPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // 021 T450 (FR-217): the data → training hand-off — ?ds=name@version prefills the pin.
    const ds = params.get('ds');
    gwGet<{ datasets: Dataset[] }>('datasets')
      .then((d) => {
        setDatasets(d.datasets || []);
        const all = (d.datasets || []).flatMap((x) =>
          x.versions.map((v) => `${x.name}@${v.version}`),
        );
        if (ds && all.includes(ds)) setDsKey(ds);
        else if (all[0]) setDsKey(all[0]);
      })
      .catch(() => setDatasets([]));
    // 021 T452/T445: lineage drill-back — ?run=<id> polls an existing run's detail (GET /runs/:id).
    const runId = params.get('run');
    if (runId) pollRun(runId);
    return () => {
      esRef.current?.close();
      if (pollRef.current) clearInterval(pollRef.current);
      if (runPollRef.current) clearInterval(runPollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 021 T452 (FR-221): lease-aware launch — read serving/state so the operator sees a refusal
  // coming BEFORE hitting launch (Principle II: the trainer refuses while a model is resident).
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<ServingState>('serving/state')
        .then((s) => alive && setLease(s))
        .catch(() => alive && setLease(null));
    tick();
    const id = setInterval(tick, 4000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [log]);

  const watch = useCallback((runId: string) => {
    esRef.current?.close();
    const es = new EventSource(`/api/gw/runs/${encodeURIComponent(runId)}/events`);
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as RunRec & { event?: string };
        if (data.event === 'run') {
          setRec(data);
          setLog((l) => [...l, `[${new Date().toLocaleTimeString()}] status=${data.status}`]);
          if (data.status && TERMINAL.has(data.status)) es.close();
        }
      } catch {
        /* ignore */
      }
    };
    es.onerror = () => es.close();
  }, []);

  // 021 T452 (FR-221): polled run detail via the newly allow-listed GET /runs/:id — used for the
  // ?run= drill-back (models lineage → this run), where the SSE bridge may already be closed.
  const pollRun = useCallback((runId: string) => {
    if (runPollRef.current) clearInterval(runPollRef.current);
    const tick = async () => {
      try {
        const r = await gwGet<RunRec>(`runs/${encodeURIComponent(runId)}`);
        setRec({ run_id: runId, ...r });
        setLog((l) =>
          l.length === 0
            ? [`[${new Date().toLocaleTimeString()}] watching ${runId} (drill-back)`]
            : l,
        );
        if (r.status && TERMINAL.has(r.status) && runPollRef.current) {
          clearInterval(runPollRef.current);
          runPollRef.current = null;
        }
      } catch {
        /* transient — keep polling */
      }
    };
    tick();
    runPollRef.current = setInterval(tick, 4000);
  }, []);

  // 012 — poll an HPO study's status (studies/{id} is a plain GET, not SSE) until it finishes.
  const watchStudy = useCallback((studyId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    const tick = async () => {
      try {
        const s = await gwGet<StudyRec>(`studies/${encodeURIComponent(studyId)}`);
        setStudy(s);
        if (s.status && TERMINAL.has(s.status) && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        /* transient — keep polling */
      }
    };
    tick();
    pollRef.current = setInterval(tick, 4000);
  }, []);

  // 021 T452 (FR-222): 409 (lease busy) and 507 (over VRAM budget) are DISTINCT first-class
  // refusals, not generic errors — each says what happened and what to do next.
  const classifyRefusal = (e: unknown, what: string): boolean => {
    if (e instanceof GwError && e.status === 409) {
      setRefusal(
        `Refused (409, lease busy): one model in VRAM at a time (Principle II). A serving model is ` +
          `resident or another ${what} is active. Let it release (idle timeout) and retry.`,
      );
      return true;
    }
    if (e instanceof GwError && e.status === 507) {
      setRefusal(
        'Refused (507, over budget): the requested base model does not fit the VRAM budget. ' +
          'Pick a smaller base or free the budget — this is admission control, not a failure.',
      );
      return true;
    }
    return false;
  };

  const launch = async () => {
    if (!dsKey || !outputName.trim()) return;
    const [dataset_name, dataset_version] = dsKey.split('@');
    setLaunching(true);
    setErr('');
    setRefusal('');
    setRec(null);
    setStudy(null);
    setLog([]);

    // 012 — optimize mode: launch an HPO study (N sequential trials) instead of a single run.
    if (optimize) {
      const sbody: Record<string, unknown> = {
        dataset_name,
        dataset_version,
        output_name: outputName,
        modality,
        seed,
        n_trials: nTrials,
      };
      if (baseModel.trim() && !LOCKED_BASE.has(modality)) sbody.base_model = baseModel.trim();
      try {
        const res = await gwPost<{ study_id: string; status: string }>('studies', sbody);
        setStudy({ study_id: res.study_id, status: res.status });
        setLog([
          `[${new Date().toLocaleTimeString()}] launched study ${res.study_id} ` +
            `(${nTrials} trials, sequential on the one GPU)`,
        ]);
        watchStudy(res.study_id);
      } catch (e) {
        if (!classifyRefusal(e, 'run/study')) setErr(String(e));
      } finally {
        setLaunching(false);
      }
      return;
    }
    // Only send the knobs the chosen modality uses; the trainer fills the rest from each flow's
    // defaults. Blank base_model / parent_version are omitted so the flow's own defaults apply.
    const body: Record<string, unknown> = {
      dataset_name,
      dataset_version,
      output_name: outputName,
      modality,
      seed,
    };
    // Vision's base is read-only in the form (locked arch) — never forwarded (FR-220).
    if (baseModel.trim() && !LOCKED_BASE.has(modality)) body.base_model = baseModel.trim();
    // Chaining is only supported for vision + embeddings (their registered artifact reloads as a
    // trainable warm start); LLM serves a GGUF and ASR a ggml binary — neither is a trainable
    // checkpoint. Never forward parent_version outside the resumable modalities (field hidden too).
    if (parentVersion.trim() && CHAINABLE.has(modality)) body.parent_version = parentVersion.trim();
    if (modality === 'llm') {
      body.steps = steps;
      body.lora_r = loraR;
    } else {
      body.epochs = epochs;
      if (modality === 'asr') body.lora_r = loraR;
    }
    try {
      const res = await gwPost<{ run_id: string; status: string }>('runs', body);
      setRec({ run_id: res.run_id, status: res.status });
      setLog([`[${new Date().toLocaleTimeString()}] launched ${res.run_id} (${res.status})`]);
      watch(res.run_id);
    } catch (e) {
      if (!classifyRefusal(e, 'run')) setErr(String(e));
    } finally {
      setLaunching(false);
    }
  };

  const dsOptions = datasets.flatMap((d) =>
    d.versions.map((v) => ({ key: `${d.name}@${v.version}`, label: `${d.name} @ ${v.version}` })),
  );
  const running = rec?.status && !TERMINAL.has(rec.status);
  const studyRunning = study?.status && !TERMINAL.has(study.status);
  const leaseHeld = !!lease?.holder && lease.holder !== 'training';

  return (
    <>
      <PageTitle sub="Launch a fine-tune (LLM · vision · embeddings · ASR) on a pinned dataset version and watch it live.">
        training
      </PageTitle>

      <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
        <Panel title="launch" hint="POST /runs">
          {/* FR-221: the lease state, read BEFORE launching — a refusal should never surprise */}
          {lease !== null && (
            <p className="mb-3 text-caption-md">
              {leaseHeld ? (
                <span className="st-warning">
                  [!] GPU lease held by {lease.holder}
                  {lease.resident ? ` (${lease.serving_model} resident)` : ''} — a launch now will be
                  refused (409) until it releases.
                </span>
              ) : lease.holder === 'training' ? (
                <span className="st-accent">[~] a training run already holds the GPU.</span>
              ) : (
                <span className="text-ash">[ ] GPU lease idle — clear to launch.</span>
              )}
            </p>
          )}
          <Field label="dataset @ version (pinned)">
            <select
              value={dsKey}
              onChange={(e) => setDsKey(e.target.value)}
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            >
              {dsOptions.length === 0 && <option value="">(no datasets)</option>}
              {dsOptions.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="output name">
            <input
              value={outputName}
              onChange={(e) => setOutputName(e.target.value)}
              placeholder="my-lora-v1"
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="modality">
              <select
                value={modality}
                onChange={(e) => setModality(e.target.value as Modality)}
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
              >
                {MODALITIES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label={
                LOCKED_BASE.has(modality) ? 'base model (locked arch)' : 'base model (pinned default)'
              }
            >
              <input
                value={LOCKED_BASE.has(modality) ? '' : baseModel}
                onChange={(e) => setBaseModel(e.target.value)}
                disabled={LOCKED_BASE.has(modality)}
                placeholder={
                  LOCKED_BASE.has(modality)
                    ? 'pinned — the head is sized to the arch'
                    : '(flow default)'
                }
                title={
                  LOCKED_BASE.has(modality)
                    ? 'vision trains a classifier head on a locked architecture — the base is not a knob'
                    : undefined
                }
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash disabled:opacity-60"
              />
            </Field>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {modality === 'llm' ? (
              <>
                <Field label="steps">
                  <NumberInput value={steps} onChange={setSteps} min={1} />
                </Field>
                <Field label="lora_r">
                  <NumberInput value={loraR} onChange={setLoraR} min={1} />
                </Field>
              </>
            ) : (
              <>
                <Field label="epochs">
                  <NumberInput value={epochs} onChange={setEpochs} min={1} />
                </Field>
                {modality === 'asr' && (
                  <Field label="lora_r">
                    <NumberInput value={loraR} onChange={setLoraR} min={1} />
                  </Field>
                )}
              </>
            )}
            <Field label="seed">
              <NumberInput value={seed} onChange={setSeed} min={0} />
            </Field>
          </div>
          {/* 012 — HPO: optimize mode searches a per-modality space across N sequential trials,
              optimizing 011's eval metric, and registers the best trial as a promotable version. */}
          <div className="mt-1 mb-3 hairline rounded-sm p-2">
            <label className="flex items-center gap-2 text-caption-md text-ink">
              <input
                type="checkbox"
                checked={optimize}
                onChange={(e) => setOptimize(e.target.checked)}
              />
              optimize hyperparameters (HPO study)
            </label>
            {optimize && (
              <div className="mt-2 grid grid-cols-2 gap-2">
                <Field label="trials (sequential)">
                  <NumberInput value={nTrials} onChange={setNTrials} min={1} />
                </Field>
                <p className="self-end text-caption-md text-ash">
                  ~{nTrials}× one train each — runs on the single GPU
                </p>
              </div>
            )}
          </div>
          {/* Chaining is supported only for the resumable modalities (vision/embeddings); hidden for
              llm/asr, which register a serving GGUF/ggml rather than a trainable checkpoint. Parent
              chaining doesn't apply to an HPO study (it trains from base each trial). */}
          {!optimize && CHAINABLE.has(modality) && (
            <Field label="parent version (optional — chain from a prior version)">
              <input
                value={parentVersion}
                onChange={(e) => setParentVersion(e.target.value)}
                placeholder="(none — train from base)"
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
              />
            </Field>
          )}
          <button
            onClick={launch}
            disabled={launching || !dsKey || !outputName.trim() || !!running || !!studyRunning}
            className="mt-2 rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
          >
            {launching ? '[~] launching…' : optimize ? '[+] launch study' : '[+] launch run'}
          </button>
          {refusal && <p className="mt-3 text-caption-md st-warning">[!] {refusal}</p>}
          {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}
        </Panel>

        <Panel
          title={study ? 'hpo study' : 'live run'}
          hint={study ? 'GET /studies/{id}' : 'GET /runs/{id}/events (SSE) · GET /runs/{id}'}
        >
          {study && <StudyView study={study} />}
          {!study && !rec && <p className="text-body-md text-mute">[ ] no active run.</p>}
          {!study && rec && (
            <>
              <div className="mb-3 flex items-center justify-between">
                <span className="text-body-strong text-ink">{rec.run_id}</span>
                <Badge
                  tone={
                    rec.status === 'completed'
                      ? 'success'
                      : rec.status === 'failed'
                        ? 'danger'
                        : 'accent'
                  }
                >
                  {rec.status}
                </Badge>
              </div>

              {/* dark live-log surface */}
              <div
                ref={logRef}
                className="console console-elevated mb-3 h-40 overflow-y-auto rounded-none p-3 text-caption-md leading-relaxed"
              >
                {log.map((l, i) => (
                  <div key={i} className="whitespace-pre-wrap">
                    {l}
                  </div>
                ))}
                {running && <div className="cursor" />}
              </div>

              {rec.metrics && Object.keys(rec.metrics).length > 0 && (
                <p className="mb-2 text-caption-md text-mute">
                  metrics:{' '}
                  {Object.entries(rec.metrics)
                    .map(([k, v]) => `${k}=${String(v)}`)
                    .join(' · ')}
                </p>
              )}
              {rec.error && <p className="text-caption-md st-danger">[x] {rec.error}</p>}

              {rec.status === 'completed' && rec.model && (
                <div className="text-caption-md">
                  <p className="st-success">
                    [✓] registered {rec.model.name} v{rec.model.version}
                  </p>
                  <Link href="/models" className="st-accent underline">
                    [→] view in models (promote it there)
                  </Link>
                </div>
              )}
            </>
          )}
        </Panel>
      </div>
    </>
  );
}

// 012 US3 — the minimal HPO surface: study status + best-trial (winning params + winning metric).
// Full live per-trial visualization is a documented fast-follow.
function StudyView({ study }: { study: StudyRec }) {
  const done = TERMINAL.has(study.status || '');
  const best = study.best;
  return (
    <>
      <div className="mb-3 flex items-center justify-between">
        <span className="text-body-strong text-ink">{study.study_id}</span>
        <Badge
          tone={study.status === 'completed' ? 'success' : study.status === 'failed' ? 'danger' : 'accent'}
        >
          {study.status}
        </Badge>
      </div>
      {study.summary?.n_trials != null && (
        <p className="mb-2 text-caption-md text-mute">
          {study.summary.completed ?? 0}/{study.summary.n_trials} trials completed · sequential on the
          one GPU
        </p>
      )}
      {!done && <p className="text-caption-md text-ash">[~] optimizing… (each trial is a full train)</p>}
      {study.error && <p className="text-caption-md st-danger">[x] {study.error}</p>}
      {best ? (
        <div className="hairline rounded-sm p-3 text-caption-md">
          <p className="st-success">
            [✓] best trial → registered v{best.version}
            {best.metric ? ` · ${best.metric}=${best.value}` : ` · objective=${best.value}`}
          </p>
          <p className="mt-1 text-ash">
            winning params:{' '}
            {Object.entries(best.params)
              .map(([k, v]) => `${k}=${String(v)}`)
              .join(' · ')}
          </p>
          <Link href="/models" className="st-accent underline">
            [→] view in models (promote it there)
          </Link>
        </div>
      ) : (
        done && <p className="text-caption-md text-ash">[ ] no best trial (all trials failed).</p>
      )}
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

function NumberInput({
  value,
  onChange,
  min,
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number;
}) {
  return (
    <input
      type="number"
      value={value}
      min={min}
      onChange={(e) => onChange(Number(e.target.value))}
      className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
    />
  );
}
