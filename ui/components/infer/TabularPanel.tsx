'use client';

import { useCallback, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';
import type { PanelProps } from './types';

type Pred = { prediction: number; score: number };
type PredictResp = { predictions: Pred[]; features?: string[]; device?: string; model?: string };

// tabular renderer (009 US4): rows of feature JSON → one prediction per row. ALWAYS live — the tabular
// service is off the GPU lease, so this never disables on lease state (FR-082), like embed. The input
// is a JSON array of row objects (missing features default to 0 server-side).
const SAMPLE = '[\n  { "f0": 1.2, "f1": -0.4, "f2": 0.3, "f3": 0.0 },\n  { "f0": -1.0, "f1": 2.1 }\n]';

export function TabularPanel({ entry }: PanelProps) {
  const [raw, setRaw] = useState(SAMPLE);
  const [resp, setResp] = useState<PredictResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const run = useCallback(async () => {
    if (busy) return;
    setErr('');
    setResp(null);
    let rows: unknown;
    try {
      rows = JSON.parse(raw);
    } catch (e) {
      setErr(`invalid JSON: ${e}`);
      return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
      setErr('expected a non-empty JSON array of row objects');
      return;
    }
    setBusy(true);
    try {
      const res = await gwPost<PredictResp>('predict', { rows });
      setResp(res);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }, [raw, busy]);

  const modelLabel = `${entry.model}${entry.version ? `@v${entry.version}` : ''}`;

  return (
    <Panel title="predict" hint="POST /predict → labels">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-caption-md">
        <span className="text-mute">model:</span>
        <span className="hairline rounded-sm bg-soft px-2 py-1 text-ink">{modelLabel}</span>
        <span className="st-accent">· CPU · always-on (off-lease)</span>
      </div>

      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) run();
        }}
        rows={6}
        spellCheck={false}
        placeholder='[{ "f0": 1.0, "f1": 0.0 }]  (⌘/Ctrl+Enter to predict)'
        className="hairline mb-2 w-full rounded-sm bg-soft p-3 font-mono text-body-md text-ink placeholder:text-ash"
      />
      <button
        onClick={run}
        disabled={busy || !raw.trim()}
        className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        [+] predict
      </button>

      <div className="mt-3 text-caption-md">
        {busy && <span className="st-accent">[~] predicting…</span>}
        {err && <span className="st-danger">[x] {err}</span>}
        {resp && (
          <ul className="space-y-1">
            {resp.predictions.map((p, i) => (
              <li key={i} className="flex items-baseline justify-between gap-3">
                <span className="text-ink">
                  <span className="st-mute">[{i}]</span> class {p.prediction}
                </span>
                <span className="text-mute">score {p.score.toFixed(3)}</span>
              </li>
            ))}
            {resp.features && <li className="pt-1 text-ash">features: {resp.features.join(', ')}</li>}
          </ul>
        )}
      </div>
    </Panel>
  );
}
