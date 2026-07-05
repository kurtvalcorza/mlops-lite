'use client';

// 021 T447 (FR-228/229, FR-250): the promote gate as the models centerpiece — a preview→promote
// flow per version. Preview = the offline champion↔challenger compare (logged metrics, NO alias
// move); promote = the gated alias move; a `blocked` verdict leaves the alias put and shows the
// verdict; override is a deliberate, separate act behind the shared ConfirmDialog REQUIRING a
// typed reason. Accepts the ?override=<name>@<version> deep-link from the retraining inbox: the
// candidate row is highlighted and framed for override review (the gate still runs — nothing
// auto-fires).

import { useState } from 'react';
import { Badge } from '@/components/Badge';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { gwPost } from '@/lib/gw';
import { LineageLinks } from './LineageLinks';

export type Version = {
  version: string;
  source: string;
  run_id: string;
  tags: Record<string, string>;
  serving: boolean;
};

type MetricBrief = { version: string; metric?: string; value?: number } | null;
export type Verdict = {
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
type CompareLeg = { version?: string; metric?: string; value?: number } | null;
type CompareRes = {
  champion?: CompareLeg;
  challenger?: CompareLeg;
  winner?: string;
  metric?: string;
};

const VERDICT_TONE: Record<Verdict['verdict'], 'success' | 'warning' | 'danger'> = {
  pass: 'success',
  warn: 'warning',
  blocked: 'danger',
};

// 011: a version's logged eval metric lives in its registry tags (written by the harness).
function evalOf(tags: Record<string, string>): string | null {
  if (!tags?.eval_metric || tags?.eval_value === undefined) return null;
  const dir = tags.eval_direction === 'lower' ? '↓' : '↑';
  return `${tags.eval_metric}=${tags.eval_value} ${dir}`;
}

export function PromoteGate({
  name,
  versions,
  championVersion,
  overrideVersion,
  onChanged,
}: {
  name: string;
  versions: Version[];
  championVersion: string | null;
  /** the ?override= deep-link candidate for THIS model (retraining → models hand-off) */
  overrideVersion: string | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [preview, setPreview] = useState<{ version: string; res: CompareRes } | null>(null);
  const [askOverride, setAskOverride] = useState<string | null>(null); // version pending override

  const doPreview = async (version: string) => {
    setBusy(version);
    setErr('');
    setVerdict(null);
    setPreview(null);
    try {
      const res = await gwPost<CompareRes>(`models/${encodeURIComponent(name)}/compare`, {
        challenger: version,
      });
      setPreview({ version, res });
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  const promote = async (version: string, override = false) => {
    setBusy(version);
    setErr('');
    setPreview(null);
    try {
      const res = await gwPost<PromoteResult>(`models/${encodeURIComponent(name)}/promote`, {
        version,
        override,
      });
      setVerdict(res.verdict);
      onChanged();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <>
      {err && <p className="mt-2 text-caption-md st-danger">[x] {err}</p>}

      {preview && (
        <div className="mt-3 hairline rounded-sm p-3 text-caption-md">
          <p className="text-ink">
            <span className="st-accent">[⇄] preview</span> — no alias move:{' '}
            {preview.res.metric ?? 'metric'}: champion v
            {preview.res.champion?.version ?? championVersion ?? '?'} ={' '}
            {preview.res.champion?.value != null ? preview.res.champion.value : '?'} vs candidate v
            {preview.res.challenger?.version ?? preview.version} ={' '}
            {preview.res.challenger?.value != null ? preview.res.challenger.value : '?'}
            {preview.res.winner ? ` · winner: ${preview.res.winner}` : ''}
          </p>
          <p className="mt-1 text-ash">
            promoting runs the same comparison through the gate — pass/warn moves the alias, a
            block leaves it put.
          </p>
        </div>
      )}

      {verdict && (
        <GateVerdictBox verdict={verdict} busy={busy} onOverride={(v) => setAskOverride(v)} />
      )}

      <ul className="mt-3 divide-y divide-hairline">
        {versions.map((v) => {
          const metric = evalOf(v.tags);
          const isOverrideTarget = overrideVersion === v.version && !v.serving;
          return (
            <li
              key={v.version}
              className={
                'py-2 ' + (isOverrideTarget ? 'hairline rounded-sm bg-soft px-2' : '')
              }
            >
              <div className="flex items-center justify-between gap-3">
                <span className="text-body-md text-ink">
                  <span className={v.serving ? 'st-accent' : 'st-mute'}>[{v.serving ? '✓' : ' '}]</span>{' '}
                  v{v.version}
                  {v.serving && <span className="ml-2 text-caption-md st-accent">@serving</span>}
                  {v.tags?.kind && <span className="ml-2 text-caption-md text-ash">{v.tags.kind}</span>}
                  {metric ? (
                    <span className="ml-2 text-caption-md st-accent">{metric}</span>
                  ) : (
                    <span className="ml-2 text-caption-md text-ash">[ ] not evaluated</span>
                  )}
                  {isOverrideTarget && (
                    <span className="ml-2 text-caption-md st-warning">
                      [!] override review (from retraining)
                    </span>
                  )}
                </span>
                <span className="flex gap-2">
                  <button
                    onClick={() => doPreview(v.version)}
                    disabled={busy === v.version || v.serving || !championVersion}
                    title={championVersion ? 'compare vs champion — no alias move' : 'no champion yet'}
                    className="hairline rounded-sm px-3 py-1 text-button-md text-mute disabled:opacity-40"
                  >
                    [⇄] preview
                  </button>
                  <button
                    onClick={() => promote(v.version)}
                    disabled={v.serving || busy === v.version}
                    className="hairline rounded-sm px-3 py-1 text-button-md text-ink disabled:opacity-40"
                  >
                    {v.serving ? 'serving' : busy === v.version ? '[~]…' : '[+] promote'}
                  </button>
                  {isOverrideTarget && (
                    <button
                      onClick={() => setAskOverride(v.version)}
                      disabled={busy === v.version}
                      className="hairline rounded-sm px-3 py-1 text-button-md st-danger disabled:opacity-40"
                    >
                      [!] override…
                    </button>
                  )}
                </span>
              </div>
              <div className="mt-0.5">
                <LineageLinks runId={v.run_id} tags={v.tags} />
              </div>
            </li>
          );
        })}
      </ul>

      <ConfirmDialog
        open={askOverride !== null}
        title="override the promote gate"
        tone="danger"
        requireReason
        reasonLabel="reason (required — why ship past a block?)"
        body={
          <>
            Promoting <span className="text-ink">{name} v{askOverride}</span> past the gate moves{' '}
            <span className="text-ink">@serving</span> to a candidate the gate would refuse
            (regression or missing metric). The alias moves immediately.
          </>
        }
        confirmLabel="override + promote"
        onConfirm={() => {
          const v = askOverride;
          setAskOverride(null);
          if (v) promote(v, true);
        }}
        onCancel={() => setAskOverride(null)}
      />
    </>
  );
}

// 011 US2 (FR-105/SC-067) → 021: the gate verdict from the last promotion — candidate vs incumbent
// metric, delta, mode. A blocked promotion offers the override path (confirm + typed reason).
function GateVerdictBox({
  verdict,
  busy,
  onOverride,
}: {
  verdict: Verdict;
  busy: string;
  onOverride: (version: string) => void;
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
      {verdict.verdict === 'blocked' && (
        <p className="mt-1 text-caption-md text-ash">the alias did NOT move — @serving is unchanged.</p>
      )}
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
          {busy === cand.version ? '[~]…' : '[!] override + promote anyway…'}
        </button>
      )}
    </div>
  );
}
