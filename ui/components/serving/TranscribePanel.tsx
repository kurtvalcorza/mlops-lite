'use client';

import { useCallback, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';
import type { PanelProps } from './types';

type TranscribeResp = { text: string; model?: string; infer_ms?: number; load_ms?: number };

// asr renderer (009 US3): drop an audio clip → transcript via whisper.cpp. Lease-governed (whisper.cpp
// is a GPU-lease tenant) — when another GPU tenant holds the lease, transcribe is disabled with a hint
// (symmetric to classify, A1 disable-with-hint). ASR holding the lease itself is fine.
export function TranscribePanel({ serving }: PanelProps) {
  const [text, setText] = useState<string | null>(null);
  const [meta, setMeta] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [fileName, setFileName] = useState('');

  const heldByOther = !!serving?.holder && serving.holder !== 'asr';
  const heldHint =
    serving?.holder === 'llm'
      ? 'GPU busy: LLM resident'
      : serving?.holder === 'vision'
        ? 'GPU busy: vision resident'
        : serving?.holder === 'training'
          ? 'GPU busy: training run active'
          : `GPU busy: ${serving?.holder} resident`;

  const handleFile = useCallback(
    async (file: File) => {
      if (heldByOther) return; // belt-and-suspenders: the input is disabled when held
      setErr('');
      setText(null);
      setMeta('');
      setFileName(file.name);
      setBusy(true);
      try {
        const b64 = await toBase64(file);
        const res = await gwPost<TranscribeResp>('transcribe', { audio_b64: b64, filename: file.name });
        setText(res.text ?? '');
        setMeta(
          `${res.model ?? 'whisper'}${res.infer_ms ? ` · ${Math.round(res.infer_ms)}ms` : ''}`,
        );
      } catch (e) {
        setErr(String(e));
      } finally {
        setBusy(false);
      }
    },
    [heldByOther],
  );

  return (
    <Panel title="transcribe" hint="POST /transcribe → text">
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
          accept="audio/*"
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
            <span className="mt-1 text-ash">free the GPU (idle-release or stop serving) to transcribe</span>
          </>
        ) : (
          <>
            <span className="st-accent">[+]</span>
            <span className="mt-1">drop an audio clip or click</span>
            {fileName && <span className="mt-1 text-ash">{fileName}</span>}
          </>
        )}
      </label>

      <div className="mt-3 text-caption-md">
        {busy && <span className="st-accent">[~] transcribing…</span>}
        {err && <span className="st-danger">[x] {err}</span>}
        {text !== null && !err && (
          <div className="space-y-1">
            <p className="whitespace-pre-wrap text-ink">{text || '(no speech detected)'}</p>
            {meta && <p className="text-ash">[✓] {meta}</p>}
          </div>
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
