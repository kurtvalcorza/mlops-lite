'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Panel } from '@/components/Panel';
import { streamSse } from '@/lib/sse';
import type { PanelProps } from './types';

// text-generation renderer (009 US1): the SSE streaming console. Lease-governed — when vision/
// training holds the GPU lease a stream would be refused, so send is disabled with a hint (008 US3).
export function StreamPanel({ entry, serving }: PanelProps) {
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
          setMeta(`model ${ev.model} · ${ev.tokens} tok · ${Math.round(Number(ev.infer_ms))}ms`);
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
  // GGUF always serves the stream, so there is no model dropdown. The version comes from the registry
  // @serving pointer (entry), the holder/resident from the live lease state.
  const modelLabel = `${entry.model}${entry.version ? `@v${entry.version}` : ''}`;

  return (
    <Panel title="stream" hint="POST /infer/stream → SSE">
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
