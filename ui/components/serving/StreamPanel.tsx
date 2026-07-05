'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';
import { streamSse } from '@/lib/sse';
import type { PanelProps } from './types';

// text-generation renderer — 021 T433 (FR-232/233): a stream/trace mode split.
//   stream (POST /infer/stream): live tokens + registry version (resolved from serving/state) +
//     cold-load ms — NO prediction id (streamed predictions log no id and skip capture, 016; they
//     are champion-unscorable, so no label hand-off is offered).
//   trace  (POST /infer): single-shot completion + registry_version + prediction_id + load_ms —
//     the path that logs the prediction AND captures the input, i.e. the one that feeds
//     monitoring; offers the "label this prediction" hand-off (FR-237, deep-link R7).
// 021 T432 (FR-235/250): when another *serving* tenant (vision/asr) holds the lease, send becomes
// a preemptive swap gated behind the shared ConfirmDialog naming the holder to evict. A training
// or job holder is NEVER presented as preemptable (Principle II sanctions only the serving swap).

// Serving tenants the agent may evict on preempt=true; training + kind="job" holders never swap.
const SWAPPABLE = new Set(['vision', 'asr']);

type Mode = 'stream' | 'trace';

type TraceResp = {
  status: string;
  registry_model: string;
  registry_version: string | null;
  prediction_id: string;
  text?: string;
  model?: string;
  load_ms?: number;
  infer_ms?: number;
};

export function StreamPanel({ entry, serving }: PanelProps) {
  const [mode, setMode] = useState<Mode>('stream');
  const [prompt, setPrompt] = useState('');
  const [tokens, setTokens] = useState('');
  const [trace, setTrace] = useState<TraceResp | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'streaming' | 'error'>('idle');
  const [meta, setMeta] = useState<string>('');
  const [err, setErr] = useState('');
  const [askSwap, setAskSwap] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const tailRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    tailRef.current?.scrollTo({ top: tailRef.current.scrollHeight });
  }, [tokens]);

  const holder = serving?.holder ?? null;
  const heldByOther = !!holder && holder !== 'llm';
  // Only a resident *serving* tenant is swappable; training/jobs keep the disabled-with-hint.
  const swappable = heldByOther && SWAPPABLE.has(holder);
  const blocked = heldByOther && !swappable;

  const runStream = useCallback(
    async (preempt: boolean) => {
      setTokens('');
      setTrace(null);
      setMeta('');
      setErr('');
      setStatus('loading');
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        for await (const ev of streamSse(
          'infer/stream',
          { prompt, max_tokens: 256, temperature: 0.7, preempt },
          ac.signal,
        )) {
          if (ev.event === 'start') {
            setStatus('streaming');
            setMeta(
              `model ${ev.model} · cold-load ${Math.round(Number(ev.load_ms))}ms` +
                (serving?.serving_version ? ` · registry v${serving.serving_version}` : ''),
            );
          } else if (ev.event === 'token') {
            setTokens((t) => t + String(ev.text ?? ''));
          } else if (ev.event === 'done') {
            setMeta(
              `model ${ev.model} · ${ev.tokens} tok · ${Math.round(Number(ev.infer_ms))}ms` +
                (serving?.serving_version ? ` · registry v${serving.serving_version}` : '') +
                ' · no prediction id (stream)',
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
    },
    [prompt, serving?.serving_version],
  );

  const runTrace = useCallback(
    async (preempt: boolean) => {
      setTokens('');
      setTrace(null);
      setMeta('');
      setErr('');
      setStatus('loading');
      try {
        const res = await gwPost<TraceResp>('infer', {
          prompt,
          max_tokens: 256,
          temperature: 0.7,
          preempt,
        });
        setTrace(res);
        setStatus('idle');
      } catch (e) {
        setErr(String(e));
        setStatus('error');
      }
    },
    [prompt],
  );

  const run = useCallback(
    (preempt: boolean) => (mode === 'stream' ? runStream(preempt) : runTrace(preempt)),
    [mode, runStream, runTrace],
  );

  const busy = status === 'loading' || status === 'streaming';

  const send = () => {
    if (!prompt.trim() || busy || blocked) return;
    if (swappable) {
      setAskSwap(true); // FR-250: preempt only through the shared confirm, naming the holder
      return;
    }
    run(false);
  };

  const stop = () => abortRef.current?.abort();
  const modelLabel = `${entry.model}${entry.version ? `@v${entry.version}` : ''}`;

  return (
    <Panel title="llm" hint="stream: POST /infer/stream (SSE) · trace: POST /infer">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-caption-md">
        <span className="text-mute">serving:</span>
        <span className="hairline rounded-sm bg-soft px-2 py-1 text-ink">{modelLabel}</span>
        {serving &&
          (heldByOther ? (
            <span className={swappable ? 'st-warning' : 'st-danger'}>
              · GPU busy: {holder} resident{swappable ? ' (swappable)' : ''}
            </span>
          ) : (
            <span className={serving.resident ? 'st-accent' : 'text-ash'}>
              · {serving.resident ? 'resident' : 'idle'}
            </span>
          ))}
        {/* mode toggle — the two paths differ in what they log (016), not just in transport */}
        <span className="ml-auto flex items-center gap-1">
          {(['stream', 'trace'] as Mode[]).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              disabled={busy}
              title={
                m === 'stream'
                  ? 'live tokens — logs no prediction id, skips input capture'
                  : 'single-shot — logs the prediction + captures the input (feeds monitoring)'
              }
              className={
                'rounded-sm px-2 py-0.5 ' +
                (mode === m ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
              }
            >
              [{mode === m ? '*' : ' '}] {m}
            </button>
          ))}
        </span>
      </div>

      <div
        ref={tailRef}
        className="console console-elevated mb-3 h-64 overflow-y-auto rounded-none p-3 text-body-md leading-relaxed"
      >
        {mode === 'stream' && tokens ? (
          <span className={busy ? 'cursor whitespace-pre-wrap' : 'whitespace-pre-wrap'}>
            {tokens}
          </span>
        ) : mode === 'trace' && trace?.text ? (
          <span className="whitespace-pre-wrap">{trace.text}</span>
        ) : (
          <span className="text-ash">
            {status === 'loading' ? (
              <span className="st-accent">
                [~] {mode === 'trace' ? 'running single-shot…' : 'loading model onto GPU…'}
              </span>
            ) : (
              '> awaiting prompt'
            )}
          </span>
        )}
      </div>

      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-caption-md">
        {mode === 'trace' && trace ? (
          <span className="st-success">
            [✓] registry v{trace.registry_version ?? '?'} · prediction {trace.prediction_id}
            {trace.load_ms != null ? ` · load ${Math.round(trace.load_ms)}ms` : ''}
            {trace.infer_ms != null ? ` · ${Math.round(trace.infer_ms)}ms` : ''}
          </span>
        ) : (
          <span className={status === 'error' ? 'st-danger' : 'st-mute'}>
            {err ? `[x] ${err}` : meta ? `[✓] ${meta}` : `[ ] ${status}`}
          </span>
        )}
        {mode === 'trace' && trace && (
          <Link
            href={`/monitoring?prediction_id=${encodeURIComponent(trace.prediction_id)}`}
            className="st-accent underline"
            title="this prediction id feeds monitoring — attach its ground-truth label"
          >
            [→] label this prediction
          </Link>
        )}
      </div>

      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send();
        }}
        rows={3}
        placeholder="prompt… (⌘/Ctrl+Enter to send)"
        className="hairline mb-2 w-full rounded-sm bg-soft p-3 text-body-md text-ink placeholder:text-ash"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={send}
          disabled={busy || !prompt.trim() || blocked}
          title={
            blocked
              ? `GPU busy: ${holder} holds the lease and is never preempted`
              : swappable
                ? `evicts the resident ${holder} model (confirmation required)`
                : undefined
          }
          className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
        >
          {swappable ? '[⇄] swap & send' : '[+] send'}
        </button>
        <button
          onClick={stop}
          disabled={!busy || mode === 'trace'}
          className="hairline rounded-sm px-4 py-1 text-button-md text-mute disabled:opacity-40"
        >
          [x] stop
        </button>
        {blocked && (
          <span className="text-caption-md st-danger">
            [!] {holder} holds the GPU — wait for it to finish
          </span>
        )}
      </div>

      <ConfirmDialog
        open={askSwap}
        title="preemptive swap"
        body={
          <>
            The GPU lease is held by <span className="text-ink">{holder}</span> with a model
            resident. Sending will <span className="st-warning">evict it</span> and load the LLM
            (sequential — one model in VRAM, Principle II). The evicted engine reloads on its next
            request (~seconds).
          </>
        }
        confirmLabel={`evict ${holder ?? ''} & send`}
        onConfirm={() => {
          setAskSwap(false);
          run(true);
        }}
        onCancel={() => setAskSwap(false)}
      />
    </Panel>
  );
}
