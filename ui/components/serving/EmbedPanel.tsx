'use client';

import { useCallback, useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';
import type { PanelProps } from './types';

type EmbedResp = { vectors: number[][]; count: number; dim: number; device?: string; model?: string };

// embedding renderer (009 US2): one text per line → equal-dimension CPU vectors. ALWAYS live — the
// embeddings service is off the GPU lease, so this never disables on lease state (FR-082), unlike the
// stream/classify panels. Shows the dimension + a short preview of the first vector.
export function EmbedPanel({ entry }: PanelProps) {
  const [text, setText] = useState('');
  const [resp, setResp] = useState<EmbedResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const run = useCallback(async () => {
    const texts = text
      .split('\n')
      .map((t) => t.trim())
      .filter(Boolean);
    if (texts.length === 0 || busy) return;
    setErr('');
    setResp(null);
    setBusy(true);
    try {
      const res = await gwPost<EmbedResp>('embed', { texts });
      setResp(res);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }, [text, busy]);

  const modelLabel = `${entry.model}${entry.version ? `@v${entry.version}` : ''}`;

  return (
    <Panel title="embed" hint="POST /embed → vectors">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-caption-md">
        <span className="text-mute">model:</span>
        <span className="hairline rounded-sm bg-soft px-2 py-1 text-ink">{modelLabel}</span>
        <span className="st-accent">· CPU · always-on (off-lease)</span>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) run();
        }}
        rows={4}
        placeholder="one text per line… (⌘/Ctrl+Enter to embed)"
        className="hairline mb-2 w-full rounded-sm bg-soft p-3 text-body-md text-ink placeholder:text-ash"
      />
      <button
        onClick={run}
        disabled={busy || !text.trim()}
        className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        [+] embed
      </button>

      <div className="mt-3 text-caption-md">
        {busy && <span className="st-accent">[~] embedding…</span>}
        {err && <span className="st-danger">[x] {err}</span>}
        {resp && (
          <div className="space-y-1">
            <p className="text-ink">
              <span className="st-mute">[✓]</span> {resp.count} vector{resp.count === 1 ? '' : 's'} ·
              dim {resp.dim} · {resp.device ?? 'cpu'}
            </p>
            {resp.vectors[0] && (
              <p className="truncate text-ash">
                v0: [{resp.vectors[0].slice(0, 6).map((x) => x.toFixed(3)).join(', ')}
                {resp.vectors[0].length > 6 ? ', …' : ''}]
              </p>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}
