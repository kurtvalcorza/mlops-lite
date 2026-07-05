'use client';

// 021 T446 (FR-226/227): on-demand metrics for one model — an evaluate BUTTON (the modality
// default, one click) with an advanced disclosure to override benchmark/metric, and a
// champion↔challenger compare (pure registry lookup since 015 — no model reload, alias unmoved).

import { useState } from 'react';
import { Panel } from '@/components/Panel';
import { gwPost } from '@/lib/gw';

type EvalRes = {
  model?: string;
  version?: string;
  benchmark?: string;
  metric?: string;
  value?: number;
  direction?: string;
  [k: string]: unknown;
};
type CompareLeg = { version?: string; metric?: string; value?: number } | null;
type CompareRes = {
  champion?: CompareLeg;
  challenger?: CompareLeg;
  winner?: string;
  metric?: string;
  [k: string]: unknown;
};

export function EvaluatePanel({
  name,
  versions,
  championVersion,
}: {
  name: string;
  versions: { version: string }[];
  championVersion: string | null;
}) {
  const [target, setTarget] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [benchmark, setBenchmark] = useState('');
  const [metric, setMetric] = useState('');
  const [busy, setBusy] = useState<'eval' | 'compare' | ''>('');
  const [err, setErr] = useState('');
  const [evalRes, setEvalRes] = useState<EvalRes | null>(null);
  const [cmpRes, setCmpRes] = useState<CompareRes | null>(null);

  const withOverrides = (body: Record<string, unknown>) => {
    if (advanced && benchmark.trim()) body.benchmark = benchmark.trim();
    if (advanced && metric.trim()) body.metric = metric.trim();
    return body;
  };

  const evaluate = async () => {
    if (!target) return;
    setBusy('eval');
    setErr('');
    setEvalRes(null);
    setCmpRes(null);
    try {
      setEvalRes(
        await gwPost<EvalRes>(
          `models/${encodeURIComponent(name)}/evaluate`,
          withOverrides({ version: target }),
        ),
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  const compare = async () => {
    if (!target) return;
    setBusy('compare');
    setErr('');
    setEvalRes(null);
    setCmpRes(null);
    try {
      setCmpRes(
        await gwPost<CompareRes>(
          `models/${encodeURIComponent(name)}/compare`,
          withOverrides({ challenger: target }),
        ),
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <Panel title="evaluate" hint="score on demand — logged metric, alias unmoved">
      <div className="flex flex-wrap items-end gap-2">
        <div>
          <label className="mb-1 block text-caption-md text-mute">version</label>
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="hairline rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
          >
            <option value="">(pick)</option>
            {versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={evaluate}
          disabled={!target || busy !== ''}
          title="one click — the modality's default benchmark + primary metric"
          className="rounded-sm bg-ink px-3 py-1 text-button-md text-canvas disabled:opacity-40"
        >
          {busy === 'eval' ? '[~]…' : '[?] evaluate'}
        </button>
        <button
          onClick={compare}
          disabled={!target || busy !== '' || !championVersion}
          title={
            championVersion
              ? `challenger v${target || '?'} vs champion v${championVersion} — logged metrics, no reload`
              : 'no champion promoted yet'
          }
          className="hairline rounded-sm px-3 py-1 text-button-md text-ink disabled:opacity-40"
        >
          {busy === 'compare' ? '[~]…' : '[⇄] compare vs champion'}
        </button>
        <button onClick={() => setAdvanced(!advanced)} className="text-caption-md text-mute underline">
          [{advanced ? '−' : '+'}] advanced
        </button>
      </div>

      {advanced && (
        <div className="mt-2 grid grid-cols-2 gap-2">
          <div>
            <label className="mb-1 block text-caption-md text-mute">benchmark (blank = modality default)</label>
            <input
              value={benchmark}
              onChange={(e) => setBenchmark(e.target.value)}
              placeholder="path under benchmarks/"
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
            />
          </div>
          <div>
            <label className="mb-1 block text-caption-md text-mute">metric (blank = primary)</label>
            <input
              value={metric}
              onChange={(e) => setMetric(e.target.value)}
              placeholder="e.g. accuracy"
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
            />
          </div>
        </div>
      )}

      {err && <p className="mt-3 whitespace-pre-wrap text-caption-md st-danger">[x] {err}</p>}
      {evalRes && (
        <p className="mt-3 text-caption-md st-success">
          [✓] v{evalRes.version ?? target}: {evalRes.metric ?? 'metric'} ={' '}
          {evalRes.value != null ? evalRes.value : JSON.stringify(evalRes).slice(0, 120)}
          {evalRes.direction ? ` (${evalRes.direction} is better)` : ''}
          {evalRes.benchmark ? ` · ${evalRes.benchmark}` : ''} · logged, alias unmoved
        </p>
      )}
      {cmpRes && (
        <div className="mt-3 text-caption-md">
          <p className="text-ink">
            <span className="st-accent">[⇄]</span> {cmpRes.metric ?? 'metric'}: champion v
            {cmpRes.champion?.version ?? championVersion} ={' '}
            {cmpRes.champion?.value != null ? cmpRes.champion.value : '?'} vs challenger v
            {cmpRes.challenger?.version ?? target} ={' '}
            {cmpRes.challenger?.value != null ? cmpRes.challenger.value : '?'}
          </p>
          {cmpRes.winner && (
            <p className="st-success">[✓] winner: {cmpRes.winner} (logged metrics — no reload)</p>
          )}
        </div>
      )}
    </Panel>
  );
}
