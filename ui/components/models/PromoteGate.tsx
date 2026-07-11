'use client';

// 021 T447 (FR-228/229, FR-250): the promote gate as the models centerpiece — a preview→promote
// flow per version. Preview = the offline champion↔challenger compare (logged metrics, NO alias
// move); promote = the gated alias move; a `blocked` verdict leaves the alias put and shows the
// verdict; override is a deliberate, separate act behind the shared ConfirmDialog REQUIRING a
// typed reason. Accepts the ?override=<name>@<version> deep-link from the retraining inbox: the
// candidate row is highlighted and framed for override review (the gate still runs — nothing
// auto-fires).
//
// 022 T482 (FR-255/258/269): for a TEXT-GENERATION version the promote action IS the served-LLM
// switch (Clarifications 2026-07-05 — no separate "set serving" control). When the switch would
// displace a RESIDENT serving model, it is gated behind the shared ConfirmDialog NAMING the model
// to be displaced, and the promote is sent with `preempt: true` (the agent refuses displacement
// without it). The card shows the resident-vs-promoted delta, and the promote response's
// `serving_llm.reload` outcome (live / deferred with the agent's reason) is surfaced.

import { useEffect, useState } from 'react';
import { Badge } from '@/components/Badge';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import type { ServingState } from '@/components/serving/types';
import { gwGet, gwPost } from '@/lib/gw';
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
// 022: the go-live half of an LLM promote (pointer + agent reload) riding the promote response.
type ServingLLMResult = {
  active: string | null;
  kind?: string;
  base?: { name: string; version: string; source: string } | null;
  reload?: { status: string; reason?: string; evicted?: string | null };
  error?: string;
};
type PromoteResult = {
  promoted: boolean;
  serving_version: string | null;
  verdict: Verdict;
  serving_llm?: ServingLLMResult;
};

// A text-generation version (task tag, or the artifact-kind inference for legacy versions).
function isLLMVersion(tags: Record<string, string>): boolean {
  return (
    tags?.task === 'text-generation' || tags?.kind === 'lora-adapter' || tags?.format === 'gguf'
  );
}

// 022 FR-258: the GPU holders a served-LLM switch would DISPLACE — any resident *serving* engine
// (the llm engine itself with a different model, OR a cross-tenant vision/asr). A training/HPO/batch
// job holder is NOT here: the backend never preempts a job, so the switch is refused/deferred rather
// than confirmed. Note `resident` in serving/state is LLM-specific (whether the llm child is loaded),
// so a vision holder reports `resident:false` — gating the confirm on `holder` (not `resident`) is
// what catches the cross-tenant case (Codex F5).
const SERVING_HOLDERS = new Set<string>(['llm', 'vision', 'asr']);
function wouldDisplace(state: ServingState | null): boolean {
  return !!state && state.holder != null && SERVING_HOLDERS.has(state.holder);
}
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
  // 022: the served-LLM switch — the version pending the displacement confirm + who it displaces.
  const [askSwitch, setAskSwitch] = useState<{ version: string; displaced: string } | null>(null);
  const [servingLLM, setServingLLM] = useState<ServingLLMResult | null>(null);
  const [liveState, setLiveState] = useState<ServingState | null>(null);

  const hasLLM = versions.some((v) => isLLMVersion(v.tags));
  useEffect(() => {
    // resident-vs-promoted delta (FR-269) — only fetched for models with LLM versions
    if (!hasLLM) return;
    gwGet<ServingState>('serving/state')
      .then(setLiveState)
      .catch(() => setLiveState(null));
  }, [hasLLM, verdict]);

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

  const promote = async (version: string, override = false, preempt = false) => {
    setBusy(version);
    setErr('');
    setPreview(null);
    setServingLLM(null);
    try {
      const res = await gwPost<PromoteResult>(`models/${encodeURIComponent(name)}/promote`, {
        version,
        override,
        preempt,
      });
      setVerdict(res.verdict);
      if (res.serving_llm) setServingLLM(res.serving_llm);
      onChanged();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  // this model+version is the LLM actually serving right now (agent-reported) — a re-promote of it
  // is a no-op. Distinct from `v.serving` (this version merely holds its OWN model's @serving alias;
  // a differently-named LLM can be the active served model — Codex F7).
  const isActiveLive = (v: Version) =>
    isLLMVersion(v.tags) && v.serving && liveState?.serving_model === name;

  // 022 (FR-258): promoting an LLM version that would displace a RESIDENT serving model goes through
  // the ConfirmDialog naming the displaced model; an idle GPU promotes straight through. Gated on the
  // GPU HOLDER, not the LLM-specific `resident` flag, so a cross-tenant vision/asr holder is caught
  // too (Codex F5).
  const promoteLLMAware = async (v: Version) => {
    if (!isLLMVersion(v.tags)) return promote(v.version);
    let state: ServingState | null = null;
    try {
      state = await gwGet<ServingState>('serving/state'); // fresh — not the polled copy
    } catch {
      state = null;
    }
    setLiveState(state);
    if (wouldDisplace(state)) {
      const displaced =
        state!.holder === 'llm'
          ? `${state!.serving_model}${state!.serving_version ? ` v${state!.serving_version}` : ''}`
          : `the resident ${state!.holder} engine`;
      setAskSwitch({ version: v.version, displaced });
      return;
    }
    return promote(v.version); // idle GPU (or a job holder the backend refuses) — nothing to confirm
  };

  return (
    <>
      {err && <p className="mt-2 text-caption-md st-danger">[x] {err}</p>}

      {/* 022 FR-269: the resident-vs-promoted delta for an LLM model — what actually runs vs
          what @serving names. Agent-reported ("unknown" when the agent is unreachable). */}
      {hasLLM && liveState && (
        <p className="mt-2 text-caption-md text-ash">
          <span className="st-mute">[≡]</span> live LLM:{' '}
          <span className="text-ink">
            {liveState.serving_model}
            {liveState.serving_version ? ` v${liveState.serving_version}` : ''}
          </span>{' '}
          ({liveState.resident ? 'resident' : 'idle'})
          {championVersion && (
            <>
              {' '}
              · promoted here: <span className="text-ink">v{championVersion}</span>
            </>
          )}
          {liveState.adapter && <> · adapter {liveState.adapter} on {liveState.base}</>}
        </p>
      )}

      {/* 022: the go-live outcome of the last LLM promote — live at once, or deferred with the
          agent's reason (job holder, missing confirm), or a pointer error. Never silent. */}
      {servingLLM && (
        <p className="mt-2 text-caption-md">
          {servingLLM.error ? (
            <span className="st-danger">[x] switch: {servingLLM.error}</span>
          ) : servingLLM.reload &&
            ['loaded', 'reloaded', 'swapped', 'noop'].includes(servingLLM.reload.status) ? (
            <span className="st-accent">
              [⇄] switch: {servingLLM.reload.status === 'noop' ? 'already live' : 'live'}
              {servingLLM.reload.evicted ? ` (displaced ${servingLLM.reload.evicted})` : ''}
            </span>
          ) : (
            <span className="st-warning">
              [~] switch {servingLLM.reload?.status ?? 'pending'}
              {servingLLM.reload?.reason ? `: ${servingLLM.reload.reason}` : ''}
            </span>
          )}
        </p>
      )}

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
                    onClick={() => promoteLLMAware(v)}
                    // For an LLM version, disable only when it's the ACTIVE live model (a true
                    // no-op) — a version that holds its model's @serving alias but is NOT the active
                    // served LLM stays clickable so the operator can switch back to it (Codex F7).
                    // Non-LLM versions keep the plain @serving disable.
                    disabled={
                      (isLLMVersion(v.tags) ? isActiveLive(v) : v.serving) || busy === v.version
                    }
                    title={
                      isLLMVersion(v.tags)
                        ? 'promote = go live: moves @serving AND switches the served LLM (022)'
                        : undefined
                    }
                    className="hairline rounded-sm px-3 py-1 text-button-md text-ink disabled:opacity-40"
                  >
                    {busy === v.version
                      ? '[~]…'
                      : isActiveLive(v) || (!isLLMVersion(v.tags) && v.serving)
                        ? 'serving'
                        : isLLMVersion(v.tags) && v.serving
                          ? '[+] set live' // @serving for its model but not the active LLM
                          : '[+] promote'}
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

      {/* 022 FR-258: the served-LLM switch confirm — names the model being displaced; on confirm
          the promote carries preempt=true (the agent refuses displacement without it). */}
      <ConfirmDialog
        open={askSwitch !== null}
        title="switch the served LLM"
        tone="warning"
        body={
          <>
            Promoting <span className="text-ink">{name} v{askSwitch?.version}</span> makes it the
            live LLM <em>now</em>: a controlled sequential reload (evict → load, one model in VRAM)
            that displaces <span className="text-ink">{askSwitch?.displaced}</span>. A running
            training/HPO/batch job would refuse the switch instead — jobs are never preempted.
          </>
        }
        confirmLabel="switch + promote"
        onConfirm={() => {
          const v = askSwitch?.version;
          setAskSwitch(null);
          if (v) promote(v, false, true);
        }}
        onCancel={() => setAskSwitch(null)}
      />

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
          if (!v) return;
          // 022 (Codex F6): an override of an LLM version that would displace a resident serving
          // model must carry preempt=true — the operator already confirmed shipping past the gate,
          // which for the LLM IS the switch; without it the agent refuses the reload and the
          // override moves the alias/pointer but never goes live. Non-LLM overrides pass false.
          const ov = versions.find((x) => x.version === v);
          const preempt = !!ov && isLLMVersion(ov.tags) && wouldDisplace(liveState);
          promote(v, true, preempt);
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
