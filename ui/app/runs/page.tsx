'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';

type DsVersion = { version: string };
type Dataset = { name: string; versions: DsVersion[] };
type RunRec = {
  run_id?: string;
  status?: string;
  model?: { name: string; version: string } | null;
  metrics?: Record<string, unknown> | null;
  error?: string | null;
};

const TERMINAL = new Set(['completed', 'failed']);

// 010 — the trainer dispatches one flow per modality; the form surfaces each modality's knobs (the
// rest fall back to the flow's conservative VRAM-fitting defaults — FR-098).
const MODALITIES = ['llm', 'vision', 'embeddings', 'asr'] as const;
type Modality = (typeof MODALITIES)[number];
// Only these modalities can resume from a prior registered version (their artifact reloads as a
// trainable warm start); LLM/ASR register a serving GGUF/ggml, not a trainable checkpoint.
const CHAINABLE = new Set<Modality>(['vision', 'embeddings']);

export default function RunsPage() {
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

  const [launching, setLaunching] = useState(false);
  const [refusal, setRefusal] = useState('');
  const [err, setErr] = useState('');
  const [rec, setRec] = useState<RunRec | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    gwGet<{ datasets: Dataset[] }>('datasets')
      .then((d) => {
        setDatasets(d.datasets || []);
        const first = d.datasets?.[0];
        if (first?.versions?.[0]) setDsKey(`${first.name}@${first.versions[0].version}`);
      })
      .catch(() => setDatasets([]));
    return () => esRef.current?.close();
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

  const launch = async () => {
    if (!dsKey || !outputName.trim()) return;
    const [dataset_name, dataset_version] = dsKey.split('@');
    setLaunching(true);
    setErr('');
    setRefusal('');
    setRec(null);
    setLog([]);
    // Only send the knobs the chosen modality uses; the trainer fills the rest from each flow's
    // defaults. Blank base_model / parent_version are omitted so the flow's own defaults apply.
    const body: Record<string, unknown> = {
      dataset_name,
      dataset_version,
      output_name: outputName,
      modality,
      seed,
    };
    if (baseModel.trim()) body.base_model = baseModel.trim();
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
      const msg = String(e);
      // Principle II: the trainer refuses to start while the serving model is resident (409).
      if (msg.includes('-> 409')) {
        setRefusal(
          'Refused: one model in VRAM at a time (Principle II). A model is resident in serving, ' +
            'or another run is active. Let it release (idle timeout) and retry.',
        );
      } else {
        setErr(msg);
      }
    } finally {
      setLaunching(false);
    }
  };

  const dsOptions = datasets.flatMap((d) =>
    d.versions.map((v) => ({ key: `${d.name}@${v.version}`, label: `${d.name} @ ${v.version}` })),
  );
  const running = rec?.status && !TERMINAL.has(rec.status);

  return (
    <>
      <PageTitle sub="Launch a fine-tune (LLM · vision · embeddings · ASR) on a pinned dataset version and watch it live.">
        runs
      </PageTitle>

      <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
        <Panel title="launch" hint="POST /runs">
          <Field label="dataset @ version">
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
            <Field label="base model (optional)">
              <input
                value={baseModel}
                onChange={(e) => setBaseModel(e.target.value)}
                placeholder="(flow default)"
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
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
          {/* Chaining is supported only for the resumable modalities (vision/embeddings); hidden for
              llm/asr, which register a serving GGUF/ggml rather than a trainable checkpoint. */}
          {CHAINABLE.has(modality) && (
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
            disabled={launching || !dsKey || !outputName.trim() || !!running}
            className="mt-2 rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
          >
            {launching ? '[~] launching…' : '[+] launch run'}
          </button>
          {refusal && (
            <p className="mt-3 text-caption-md st-warning">[!] {refusal}</p>
          )}
          {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}
        </Panel>

        <Panel title="live run" hint="GET /runs/{id}/events (SSE)">
          {!rec && <p className="text-body-md text-mute">[ ] no active run.</p>}
          {rec && (
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

              {rec.error && <p className="text-caption-md st-danger">[x] {rec.error}</p>}

              {rec.status === 'completed' && rec.model && (
                <div className="text-caption-md">
                  <p className="st-success">
                    [✓] registered {rec.model.name} v{rec.model.version}
                  </p>
                  <Link href="/models" className="st-accent underline">
                    [→] promote it in models
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
