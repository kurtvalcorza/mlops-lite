'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';
import { streamSse } from '@/lib/sse';

type Pred = { label: string; score: number };
type VisionResp = { predictions: Pred[]; device?: string; model?: string };
// 008 US3 (FR-068): the gateway's lease/GPU state. `holder` ∈ {llm, vision, training, null}.
type ServingState = {
  holder: 'llm' | 'vision' | 'training' | null;
  resident: boolean;
  serving_model: string;
  serving_version: string | null;
};

/** Poll the gateway's GPU/lease state so the tab reflects what is actually resident (008 US3). */
function useServingState(intervalMs = 4000): ServingState | null {
  const [state, setState] = useState<ServingState | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<ServingState>('serving/state')
        .then((s) => alive && setState(s))
        .catch(() => {});
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return state;
}

export default function InferPage() {
  const serving = useServingState();
  return (
    <>
      <PageTitle sub="Stream a completion or classify an image. The API key stays server-side (BFF).">
        infer
      </PageTitle>
      <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <StreamConsole serving={serving} />
        <VisionDropzone serving={serving} />
      </div>
    </>
  );
}

function StreamConsole({ serving }: { serving: ServingState | null }) {
  const [prompt, setPrompt] = useState('');
  const [tokens, setTokens] = useState('');
  const [status, setStatus] = useState<'idle' | 'loading' | 'streaming' | 'error'>('idle');
  const [meta, setMeta] = useState<string>('');
  const [err, setErr] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const tailRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    tailRef.current?.scrollTo({ top: tailRef.current.scrollHeight });
  }, [tokens]);

  // When vision/training holds the GPU lease, a stream would be refused — reflect that in the status
  // and disable send (Codex #9), symmetric to the classify-disable-with-hint.
  const streamHeld = !!serving?.holder && serving.holder !== 'llm';

  const run = useCallback(async () => {
    if (!prompt.trim() || status === 'loading' || status === 'streaming' || streamHeld) return;
    setTokens('');
    setMeta('');
    setErr('');
    setStatus('loading');
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      for await (const ev of streamSse(
        'infer/stream',
        { prompt, max_tokens: 256, temperature: 0.7 },
        ac.signal,
      )) {
        if (ev.event === 'start') {
          setStatus('streaming');
          setMeta(`model ${ev.model} · cold-load ${Math.round(Number(ev.load_ms))}ms`);
        } else if (ev.event === 'token') {
          setTokens((t) => t + String(ev.text ?? ''));
        } else if (ev.event === 'done') {
          setMeta(
            `model ${ev.model} · ${ev.tokens} tok · ${Math.round(Number(ev.infer_ms))}ms`,
          );
          setStatus('idle');
        } else if (ev.event === 'error') {
          setErr(String(ev.detail ?? 'stream error'));
          setStatus('error');
        }
      }
      setStatus((s) => (s === 'streaming' ? 'idle' : s));
    } catch (e) {
      if (!ac.signal.aborted) {
        setErr(String(e));
        setStatus('error');
      } else {
        setStatus('idle');
      }
    } finally {
      abortRef.current = null;
    }
  }, [prompt, status, streamHeld]);

  const stop = () => abortRef.current?.abort();
  const busy = status === 'loading' || status === 'streaming';

  // 008 US3 (FR-069): read-only "serving: <model>@vN · resident|idle" status line — the resident
  // GGUF always serves the stream, so the old (inert) model dropdown is removed. No selection is sent.
  const modelLabel = serving
    ? `${serving.serving_model}${serving.serving_version ? `@v${serving.serving_version}` : ''}`
    : '…';

  return (
    <Panel title="stream" hint="POST /infer/stream → SSE">
      {/* read-only GPU/serving status line (replaces the inert model dropdown) */}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-caption-md">
        <span className="text-mute">serving:</span>
        <span className="hairline rounded-sm bg-soft px-2 py-1 text-ink">{modelLabel}</span>
        {serving &&
          (streamHeld ? (
            <span className="st-danger">· GPU busy: {serving.holder} resident</span>
          ) : (
            <span className={serving.resident ? 'st-accent' : 'text-ash'}>
              · {serving.resident ? 'resident' : 'idle'}
            </span>
          ))}
        <span className="text-ash">
          {streamHeld
            ? `[!] free the GPU (${serving?.holder} holds it) to stream`
            : '[i] the resident GGUF serves the stream'}
        </span>
      </div>

      {/* dark streaming console — the one raised surface */}
      <div
        ref={tailRef}
        className="console console-elevated mb-3 h-64 overflow-y-auto rounded-none p-3 text-body-md leading-relaxed"
      >
        {tokens ? (
          <span className={busy ? 'cursor whitespace-pre-wrap' : 'whitespace-pre-wrap'}>
            {tokens}
          </span>
        ) : (
          <span className="text-ash">
            {status === 'loading' ? (
              <span className="st-accent">[~] loading model onto GPU…</span>
            ) : (
              '> awaiting prompt'
            )}
          </span>
        )}
      </div>

      <div className="mb-2 flex items-center justify-between text-caption-md">
        <span className={status === 'error' ? 'st-danger' : 'st-mute'}>
          {err ? `[x] ${err}` : meta ? `[✓] ${meta}` : `[ ] ${status}`}
        </span>
      </div>

      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) run();
        }}
        rows={3}
        placeholder="prompt… (⌘/Ctrl+Enter to send)"
        className="hairline mb-2 w-full rounded-sm bg-soft p-3 text-body-md text-ink placeholder:text-ash"
      />
      <div className="flex gap-2">
        <button
          onClick={run}
          disabled={busy || !prompt.trim() || streamHeld}
          title={streamHeld ? `GPU busy: ${serving?.holder} holds the lease` : undefined}
          className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
        >
          [+] send
        </button>
        <button
          onClick={stop}
          disabled={!busy}
          className="hairline rounded-sm px-4 py-1 text-button-md text-mute disabled:opacity-40"
        >
          [x] stop
        </button>
      </div>
    </Panel>
  );
}

function VisionDropzone({ serving }: { serving: ServingState | null }) {
  const [preds, setPreds] = useState<Pred[] | null>(null);
  const [device, setDevice] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [fileName, setFileName] = useState('');

  // 008 US3 (FR-070, A1 disable-with-hint): one model in VRAM — if another tenant holds the GPU
  // lease, classify is disabled with a hint. The operator frees the GPU (idle-release or stop
  // serving) to classify. No preemption/swap (A2 deferred). Vision holding the lease is fine.
  const heldByOther = !!serving?.holder && serving.holder !== 'vision';
  const heldHint =
    serving?.holder === 'llm'
      ? 'GPU busy: LLM resident'
      : serving?.holder === 'training'
        ? 'GPU busy: training run active'
        : `GPU busy: ${serving?.holder} resident`;

  const handleFile = useCallback(
    async (file: File) => {
      if (heldByOther) return; // belt-and-suspenders: the input is disabled when held
      setErr('');
      setPreds(null);
      setFileName(file.name);
      setBusy(true);
      try {
        const b64 = await toBase64(file);
        const res = await gwPost<VisionResp>('vision/classify', { image_b64: b64 });
        setPreds(res.predictions || []);
        setDevice(res.device || '');
      } catch (e) {
        setErr(String(e));
      } finally {
        setBusy(false);
      }
    },
    [heldByOther],
  );

  return (
    <Panel title="classify" hint="POST /vision/classify → top-5">
      <label
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (heldByOther) return;
          const f = e.dataTransfer.files?.[0];
          if (f) handleFile(f);
        }}
        aria-disabled={heldByOther}
        className={
          'hairline flex h-32 flex-col items-center justify-center rounded-sm bg-soft text-caption-md text-mute ' +
          (heldByOther ? 'cursor-not-allowed opacity-40' : 'cursor-pointer')
        }
      >
        <input
          type="file"
          accept="image/*"
          className="hidden"
          disabled={heldByOther}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) handleFile(f);
          }}
        />
        {heldByOther ? (
          <>
            <span className="st-danger">[!]</span>
            <span className="mt-1">{heldHint}</span>
            <span className="mt-1 text-ash">free the GPU (idle-release or stop serving) to classify</span>
          </>
        ) : (
          <>
            <span className="st-accent">[+]</span>
            <span className="mt-1">drop an image or click</span>
            {fileName && <span className="mt-1 text-ash">{fileName}</span>}
          </>
        )}
      </label>

      <div className="mt-3 text-caption-md">
        {busy && <span className="st-accent">[~] classifying…</span>}
        {err && <span className="st-danger">[x] {err}</span>}
        {preds && (
          <ul className="space-y-1">
            {preds.map((p, i) => (
              <li key={i} className="flex items-baseline justify-between gap-3">
                <span className="text-ink">
                  <span className="st-mute">[{i === 0 ? '✓' : ' '}]</span> {p.label}
                </span>
                <span className="text-mute">{(p.score * 100).toFixed(1)}%</span>
              </li>
            ))}
            {device && <li className="pt-1 text-ash">device: {device}</li>}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result);
      resolve(s.slice(s.indexOf(',') + 1)); // strip the data: URL prefix
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
