'use client';

// 021 T443 (FR-247, research R7): the promotion-suggestions inbox — the audit trail of the
// autonomous loop. Filterable by state; accept routes through the GATED promote (a blocked verdict
// keeps the suggestion open — override deliberately does NOT live on accept; the deep-link hands
// off to the models stage where override-with-reason is the explicit, framed action).

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';

type Suggestion = {
  id: string;
  model_name: string;
  candidate_version: string;
  gate_verdict: { verdict?: string } | null;
  shadow_verdict: { winner?: string; metric?: string } | null;
  state: string;
  created_at: number;
  actor?: string | null;
};

const STATES = ['open', 'accepted', 'dismissed', 'all'] as const;
type StateFilter = (typeof STATES)[number];

export function SuggestionsInbox({ onPromoted }: { onPromoted?: () => void }) {
  const [filter, setFilter] = useState<StateFilter>('open');
  const [rows, setRows] = useState<Suggestion[] | null>(null);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [blocked, setBlocked] = useState<{ sug: Suggestion; detail: string } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const q = filter === 'all' ? 'suggestions' : `suggestions?state=${filter}`;
      const d = await gwGet<{ suggestions: Suggestion[] }>(q);
      setRows(d.suggestions || []);
    } catch (e) {
      setErr(String(e));
      setRows((r) => r ?? []);
    }
  }, [filter]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  const act = async (sug: Suggestion, action: 'accept' | 'dismiss') => {
    setBusy(sug.id);
    setErr('');
    setBlocked(null);
    try {
      const res = await gwPost<{ promoted?: boolean; detail?: string }>(
        `suggestions/${sug.id}/${action}`,
        {},
      );
      if (action === 'accept' && res.promoted === false) {
        // FR-247: the gate refused — the suggestion STAYS OPEN; offer the models-stage override
        // hand-off instead of overriding here.
        setBlocked({ sug, detail: res.detail || 'gate blocked the promotion' });
      }
      await refresh();
      if (action === 'accept' && res.promoted) onPromoted?.();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <Panel
      title="suggestions inbox"
      hint="GET /suggestions — accept promotes via the gate; a block keeps it open"
    >
      <div className="mb-3 flex items-center gap-1 text-caption-md">
        <span className="mr-1 text-mute">state:</span>
        {STATES.map((s) => (
          <button
            key={s}
            onClick={() => {
              setFilter(s);
              setRows(null);
            }}
            className={
              'rounded-sm px-2 py-0.5 ' +
              (filter === s ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
            }
          >
            [{filter === s ? '*' : ' '}] {s}
          </button>
        ))}
      </div>

      {err && <p className="mb-2 text-caption-md st-danger">[x] {err}</p>}
      {blocked && (
        <p className="mb-2 text-caption-md st-warning">
          [!] {blocked.sug.model_name} v{blocked.sug.candidate_version}: {blocked.detail} — the
          suggestion stays open.{' '}
          <Link
            href={`/models?override=${encodeURIComponent(blocked.sug.model_name)}@${encodeURIComponent(blocked.sug.candidate_version)}`}
            className="st-accent underline"
          >
            [→] review &amp; override in models
          </Link>
        </p>
      )}

      {rows === null ? (
        <p className="text-caption-md text-ash">[~] loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-body-md text-mute">[ ] no {filter === 'all' ? '' : `${filter} `}suggestions.</p>
      ) : (
        <ul className="divide-y divide-hairline">
          {rows.map((sug) => (
            <li key={sug.id} className="flex items-center justify-between gap-3 py-2 text-body-md">
              <span className="text-ink">
                <span className={sug.state === 'open' ? 'st-accent' : 'st-mute'}>
                  [{sug.state === 'open' ? '→' : sug.state === 'accepted' ? '✓' : 'x'}]
                </span>{' '}
                {sug.model_name} v{sug.candidate_version}
                <span className="text-mute">
                  {' '}
                  · gate {sug.gate_verdict?.verdict ?? '?'}
                  {sug.shadow_verdict ? ` · shadow ${sug.shadow_verdict.winner}` : ' · no shadow window'}
                  {' · '}
                  {new Date(sug.created_at * 1000).toLocaleString()}
                  {sug.actor ? ` · by ${sug.actor}` : ''}
                </span>
              </span>
              {sug.state === 'open' ? (
                <span className="flex gap-2 text-caption-md">
                  <button
                    onClick={() => act(sug, 'accept')}
                    disabled={busy === sug.id}
                    className="rounded-sm bg-ink px-3 py-0.5 text-canvas disabled:opacity-40"
                  >
                    {busy === sug.id ? '[~]' : 'accept (gated)'}
                  </button>
                  <button
                    onClick={() => act(sug, 'dismiss')}
                    disabled={busy === sug.id}
                    className="underline text-mute"
                  >
                    dismiss
                  </button>
                </span>
              ) : (
                <span className="text-caption-md text-ash">{sug.state}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
