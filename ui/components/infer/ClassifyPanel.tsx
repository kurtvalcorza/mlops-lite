'use client';

import { useCallback, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';
import type { PanelProps } from './types';

type Pred = { label: string; score: number };
type VisionResp = { predictions: Pred[]; device?: string; model?: string };

// image-classification renderer (009 US1): drop an image → top-5. Lease-governed (008 US3, A1
// disable-with-hint) — one model in VRAM, so when another GPU tenant holds the lease classify is
// disabled with a hint. The operator frees the GPU (idle-release or stop serving) to classify; no
// preemption/swap (A2 deferred). Vision holding the lease itself is fine.
export function ClassifyPanel({ serving }: PanelProps) {
  const [preds, setPreds] = useState<Pred[] | null>(null);
  const [device, setDevice] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [fileName, setFileName] = useState('');

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
