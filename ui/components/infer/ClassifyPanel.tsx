'use client';

import { useCallback, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';
import type { PanelProps } from './types';

type Pred = { label: string; score: number };
type VisionResp = { predictions: Pred[]; device?: string; model?: string };

// image-classification renderer (009 US1): drop an image → top-5. Lease-governed (008 — one model in
// VRAM). 017/A2: when another *serving* model (LLM/ASR) holds the lease, classify is no longer a dead
// end — it offers a cost-stating "Swap & classify" that, on confirm, sends preempt=true so the gateway
// evicts the resident model and loads vision (sequential, one model in VRAM). A **training** holder is
// never preemptable, so that case keeps the 008/A1 disabled-with-hint. Vision holding the lease is fine.
export function ClassifyPanel({ serving }: PanelProps) {
  const [preds, setPreds] = useState<Pred[] | null>(null);
  const [device, setDevice] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [fileName, setFileName] = useState('');

  const holder = serving?.holder;
  const heldByOther = !!holder && holder !== 'vision';
  const heldByTraining = holder === 'training';
  // A serving holder (LLM/ASR) is swappable; a training run is not (017 FR-155) → stay disabled.
  const swappable = heldByOther && !heldByTraining;
  const blocked = heldByOther && !swappable; // training holder → no swap offered
  const holderLabel = holder === 'llm' ? 'LLM' : holder === 'asr' ? 'ASR' : String(holder);

  const handleFile = useCallback(
    async (file: File, preempt = false) => {
      if (blocked) return; // training holds the GPU — never preempted
      setErr('');
      setPreds(null);
      setFileName(file.name);
      setBusy(true);
      try {
        const b64 = await toBase64(file);
        const res = await gwPost<VisionResp>('vision/classify', { image_b64: b64, preempt });
        setPreds(res.predictions || []);
        setDevice(res.device || '');
      } catch (e) {
        setErr(String(e));
      } finally {
        setBusy(false);
      }
    },
    [blocked],
  );

  // On a swappable holder, confirm the cost before sending preempt=true; otherwise classify normally.
  const onPick = useCallback(
    (file: File) => {
      if (blocked) return;
      if (swappable) {
        if (window.confirm(`Evict the resident ${holderLabel} model (~2.5s reload) and classify?`)) {
          handleFile(file, true);
        }
        return;
      }
      handleFile(file, false);
    },
    [blocked, swappable, holderLabel, handleFile],
  );

  return (
    <Panel title="classify" hint="POST /vision/classify → top-5">
      <label
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (blocked) return;
          const f = e.dataTransfer.files?.[0];
          if (f) onPick(f);
        }}
        aria-disabled={blocked}
        className={
          'hairline flex h-32 flex-col items-center justify-center rounded-sm bg-soft text-caption-md text-mute ' +
          (blocked ? 'cursor-not-allowed opacity-40' : 'cursor-pointer')
        }
      >
        <input
          type="file"
          accept="image/*"
          className="hidden"
          disabled={blocked}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onPick(f);
          }}
        />
        {blocked ? (
          <>
            <span className="st-danger">[!]</span>
            <span className="mt-1">GPU busy: training run active</span>
            <span className="mt-1 text-ash">training is never preempted — wait for it to finish</span>
          </>
        ) : swappable ? (
          <>
            <span className="st-warn">[⇄]</span>
            <span className="mt-1">Swap &amp; classify</span>
            <span className="mt-1 text-ash">evicts the resident {holderLabel} (~2.5s reload)</span>
            {fileName && <span className="mt-1 text-ash">{fileName}</span>}
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
