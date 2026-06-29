'use client';

import { useCallback, useEffect, useState } from 'react';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';

type ModelRow = { name: string; serving_version: string | null };
type Version = {
  version: string;
  source: string;
  run_id: string;
  tags: Record<string, string>;
  serving: boolean;
};
type ModelDetail = { name: string; serving: { version: string } | null; versions: Version[] };

// A candidate with no logged metric still carries its version (so a missing-metric block is
// override-able from the panel); metric/value are present only once a version has been evaluated.
type MetricBrief = { version: string; metric?: string; value?: number } | null;
type Verdict = {
  verdict: 'pass' | 'warn' | 'blocked';
  reason: string;
  flagged: boolean;
  mode: string;
  tolerance: number;
  override: boolean;
  candidate: MetricBrief;
  incumbent: MetricBrief;
  delta: number | null;
};
type PromoteResult = { promoted: boolean; serving_version: string | null; verdict: Verdict };

// 011: a version's logged eval metric lives in its registry tags (written by the harness).
function evalOf(tags: Record<string, string>): string | null {
  if (!tags?.eval_metric || tags?.eval_value === undefined) return null;
  const dir = tags.eval_direction === 'lower' ? '↓' : '↑';
  return `${tags.eval_metric}=${tags.eval_value} ${dir}`;
}

const VERDICT_TONE: Record<Verdict['verdict'], 'success' | 'warning' | 'danger'> = {
  pass: 'success',
  warn: 'warning',
  blocked: 'danger',
};

export default function ModelsPage() {
  const [models, setModels] = useState<ModelRow[]>([]);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await gwGet<{ models: ModelRow[] }>('models');
      setModels(d.models || []);
      setErr('');
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <PageTitle sub="Browse registered models and promote a version to serving.">models</PageTitle>
      {err && (
        <p className="mb-4 text-caption-md st-danger">[x] {err}</p>
      )}
      {loading && <p className="text-caption-md text-mute">[~] loading…</p>}
      <div className="space-y-3">
        {models.map((m) => (
          <ModelCard key={m.name} model={m} onPromote={load} />
        ))}
        {!loading && models.length === 0 && (
          <p className="text-body-md text-mute">[ ] no registered models.</p>
        )}
      </div>
    </>
  );
}

function ModelCard({ model, onPromote }: { model: ModelRow; onPromote: () => void }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ModelDetail | null>(null);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [verdict, setVerdict] = useState<Verdict | null>(null);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      try {
        setDetail(await gwGet<ModelDetail>(`models/${encodeURIComponent(model.name)}`));
      } catch (e) {
        setErr(String(e));
      }
    }
  };

  const promote = async (version: string, override = false) => {
    setBusy(version);
    setErr('');
    try {
      // 011: promotion is gated. The response carries the verdict + whether the alias actually moved
      // (a default hard-gate block keeps it put with promoted=false).
      const res = await gwPost<PromoteResult>(
        `models/${encodeURIComponent(model.name)}/promote`,
        { version, override },
      );
      setVerdict(res.verdict);
      setDetail(await gwGet<ModelDetail>(`models/${encodeURIComponent(model.name)}`));
      onPromote(); // refresh the Infer picker's source of truth
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <Panel>
      <button onClick={toggle} className="flex w-full items-center justify-between text-left">
        <span className="text-body-strong text-ink">
          <span className="st-mute">[{open ? '−' : '+'}]</span> {model.name}
        </span>
        {model.serving_version ? (
          <Badge tone="accent">serving @v{model.serving_version}</Badge>
        ) : (
          <span className="text-caption-md text-ash">[ ] none promoted</span>
        )}
      </button>

      {err && <p className="mt-2 text-caption-md st-danger">[x] {err}</p>}

      {verdict && <GateVerdict verdict={verdict} onOverride={(v) => promote(v, true)} busy={busy} />}

      {open && detail && (
        <ul className="mt-3 divide-y divide-hairline">
          {detail.versions.map((v) => {
            const metric = evalOf(v.tags);
            return (
              <li key={v.version} className="flex items-center justify-between gap-3 py-2">
                <span className="text-body-md text-ink">
                  <span className={v.serving ? 'st-accent' : 'st-mute'}>
                    [{v.serving ? '✓' : ' '}]
                  </span>{' '}
                  v{v.version}
                  {v.tags?.kind && <span className="ml-2 text-caption-md text-ash">{v.tags.kind}</span>}
                  {metric ? (
                    <span className="ml-2 text-caption-md st-accent">{metric}</span>
                  ) : (
                    <span className="ml-2 text-caption-md text-ash">[ ] not evaluated</span>
                  )}
                  <span className="ml-2 text-caption-md text-ash">{v.source}</span>
                </span>
                <button
                  onClick={() => promote(v.version)}
                  disabled={v.serving || busy === v.version}
                  className="hairline rounded-sm px-3 py-1 text-button-md text-ink disabled:opacity-40"
                >
                  {v.serving ? 'serving' : busy === v.version ? '[~]…' : '[+] promote'}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

// 011 US2 (FR-105/SC-067): surface the gate verdict from the last promotion — candidate vs incumbent
// metric, delta, and the mode that produced it. A blocked promotion offers an explicit override.
function GateVerdict({
  verdict,
  onOverride,
  busy,
}: {
  verdict: Verdict;
  onOverride: (version: string) => void;
  busy: string;
}) {
  const tone = VERDICT_TONE[verdict.verdict];
  const cand = verdict.candidate;
  const inc = verdict.incumbent;
  return (
    <div className="mt-3 hairline rounded-sm p-3">
      <div className="flex items-center justify-between">
        <Badge tone={tone}>gate: {verdict.verdict}</Badge>
        <span className="text-caption-md text-ash">
          mode {verdict.mode} · tol {verdict.tolerance}
        </span>
      </div>
      <p className="mt-1 text-caption-md text-mute">{verdict.reason}</p>
      {cand && inc && cand.value !== undefined && inc.value !== undefined && (
        <p className="mt-1 text-caption-md text-ash">
          candidate v{cand.version} {cand.metric}={cand.value} vs incumbent v{inc.version}{' '}
          {inc.metric}={inc.value}
          {verdict.delta !== null && <span> · Δ {verdict.delta}</span>}
        </p>
      )}
      {verdict.verdict === 'blocked' && cand && (
        <button
          onClick={() => onOverride(cand.version)}
          disabled={busy === cand.version}
          className="mt-2 hairline rounded-sm px-3 py-1 text-button-md st-danger disabled:opacity-40"
        >
          {busy === cand.version ? '[~]…' : '[!] override + promote anyway'}
        </button>
      )}
    </div>
  );
}
