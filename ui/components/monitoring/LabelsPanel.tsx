'use client';

// 021 T437 (FR-239): attach a delayed ground-truth label to a served prediction by id. Accepts the
// `?prediction_id=` deep-link from the serving stage's trace mode (R7). The write is write-once —
// late labels count, a duplicate or unknown id is reported cleanly, never overwriting history.

import { useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';

type LabelResp = { prediction_id: string; status: string; detail?: string };

export function LabelsPanel() {
  const params = useSearchParams();
  const [pid, setPid] = useState('');
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [res, setRes] = useState<LabelResp | null>(null);

  // Deep-link prefill (serving → monitoring hand-off, FR-237): read once on load.
  useEffect(() => {
    const fromLink = params.get('prediction_id');
    if (fromLink) setPid(fromLink);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = async () => {
    if (!pid.trim() || !label.trim()) return;
    setBusy(true);
    setErr('');
    setRes(null);
    try {
      // Labels are modality-agnostic: send numbers as numbers, everything else as the raw string.
      const raw = label.trim();
      const asNum = Number(raw);
      const value: unknown = raw !== '' && !Number.isNaN(asNum) ? asNum : raw;
      setRes(await gwPost<LabelResp>('monitor/labels', { prediction_id: pid.trim(), label: value }));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="labels" hint="POST /monitor/labels — ground truth by prediction id (write-once)">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1.4fr_1fr_auto]">
        <div>
          <label className="mb-1 block text-caption-md text-mute">prediction id</label>
          <input
            value={pid}
            onChange={(e) => setPid(e.target.value)}
            placeholder="from serving trace mode"
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
          />
        </div>
        <div>
          <label className="mb-1 block text-caption-md text-mute">ground-truth label</label>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="class / text / value"
            className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
          />
        </div>
        <button
          onClick={submit}
          disabled={busy || !pid.trim() || !label.trim()}
          className="self-end rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
        >
          {busy ? '[~]…' : '[+] attach'}
        </button>
      </div>

      {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}
      {res && (
        <p className="mt-3 text-caption-md">
          {res.status === 'attached' ? (
            <span className="st-success">
              [✓] label attached to {res.prediction_id} — it now counts toward the quality window
              (late arrival is fine).
            </span>
          ) : res.status === 'duplicate' ? (
            <span className="st-warning">
              [!] duplicate — {res.prediction_id} already carries a label; served history is
              write-once and was NOT overwritten.
            </span>
          ) : res.status === 'unknown' ? (
            <span className="st-warning">
              [?] unknown id — no logged prediction matches {res.prediction_id}. Streamed
              predictions log no id; use the serving trace mode.
            </span>
          ) : (
            <span className="text-mute">
              [{res.status}] {res.detail ?? ''}
            </span>
          )}
        </p>
      )}
    </Panel>
  );
}
