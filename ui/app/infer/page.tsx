'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';
import { streamSse } from '@/lib/sse';

type ModelRow = { name: string; serving_version: string | null };
type Pred = { label: string; score: number };
type VisionResp = { predictions: Pred[]; device?: string; model?: string };

export default function InferPage() {
  return (
    <>
      <PageTitle sub="Stream a completion or classify an image. The API key stays server-side (BFF).">
        infer
      </PageTitle>
      <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <StreamConsole />
        <VisionDropzone />
      </div>
    </>
  );
}

function StreamConsole() {
  const [prompt, setPrompt] = useState('');
  const [models, setModels] = useState<ModelRow[]>([]);
  const [selected, setSelected] = useState('');
  const [tokens, setTokens] = useState('');
  const [status, setStatus] = useState<'idle' | 'loading' | 'streaming' | 'error'>('idle');
  const [meta, setMeta] = useState<string>('');
  const [err, setErr] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const tailRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    gwGet<{ models: ModelRow[] }>('models')
      .then((d) => {
        setModels(d.models || []);
        const serving = (d.models || []).find((m) => m.serving_version);
        if (serving) setSelected(serving.name);
      })
      .catch(() => setModels([]));
  }, []);

  useEffect(() => {
    tailRef.current?.scrollTo({ top: tailRef.current.scrollHeight });
  }, [tokens]);

  const run = useCallback(async () => {
    if (!prompt.trim() || status === 'loading' || status === 'streaming') return;
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
  }, [prompt, status]);

  const stop = () => abortRef.current?.abort();
  const busy = status === 'loading' || status === 'streaming';

  return (
    <Panel title="stream" hint="POST /infer/stream → SSE">
      {/* model/selector line (tui-prompt-row) */}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-caption-md">
        <span className="text-mute">model:</span>
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="hairline rounded-sm bg-soft px-2 py-1 text-ink"
        >
          {models.length === 0 && <option value="">(none registered)</option>}
          {models.map((m) => (
            <option key={m.name} value={m.name}>
              {m.name}
              {m.serving_version ? ` @v${m.serving_version}` : ''}
            </option>
          ))}
        </select>
        <span className="text-ash">
          [i] the resident GGUF serves the stream; promote in models (US3) to switch
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
          disabled={busy || !prompt.trim()}
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

function VisionDropzone() {
  const [preds, setPreds] = useState<Pred[] | null>(null);
  const [device, setDevice] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [fileName, setFileName] = useState('');

  const handleFile = useCallback(async (file: File) => {
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
  }, []);

  return (
    <Panel title="classify" hint="POST /vision/classify → top-5">
      <label
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          const f = e.dataTransfer.files?.[0];
          if (f) handleFile(f);
        }}
        className="hairline flex h-32 cursor-pointer flex-col items-center justify-center rounded-sm bg-soft text-caption-md text-mute"
      >
        <input
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) handleFile(f);
          }}
        />
        <span className="st-accent">[+]</span>
        <span className="mt-1">drop an image or click</span>
        {fileName && <span className="mt-1 text-ash">{fileName}</span>}
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
