'use client';

import { Suspense, useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { EvaluatePanel } from '@/components/models/EvaluatePanel';
import { PromoteGate, type Version } from '@/components/models/PromoteGate';
import { gwGet } from '@/lib/gw';

type ModelRow = { name: string; serving_version: string | null };
type ModelDetail = { name: string; serving: { version: string } | null; versions: Version[] };

// 021 T445 (FR-224..229): the models stage — the registry with the promote GATE as its
// centerpiece: champion marked, lineage drill-back per version (seeded/imported distinct),
// evaluate on demand, preview→promote, override-with-reason. Accepts the
// ?override=<name>@<version> deep-link from the retraining inbox (R7). The suggestions inbox
// itself lives in /retraining (US4).
export default function ModelsPage() {
  return (
    <Suspense fallback={<p className="text-caption-md text-ash">[~] loading…</p>}>
      <ModelsView />
    </Suspense>
  );
}

function ModelsView() {
  const params = useSearchParams();
  const [models, setModels] = useState<ModelRow[]>([]);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  // retraining → models hand-off: a blocked candidate to review for override.
  const [overrideTarget, setOverrideTarget] = useState<{ name: string; version: string } | null>(null);
  useEffect(() => {
    const raw = params.get('override');
    if (raw && raw.includes('@')) {
      const at = raw.lastIndexOf('@');
      setOverrideTarget({ name: raw.slice(0, at), version: raw.slice(at + 1) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      <PageTitle sub="The registry, centered on the gate: preview, promote, or override with a reason.">
        models
      </PageTitle>
      {overrideTarget && (
        <p className="mb-4 text-caption-md st-warning">
          [!] override review: <span className="text-ink">{overrideTarget.name} v{overrideTarget.version}</span>{' '}
          arrived gate-blocked from retraining — its row below is highlighted; overriding requires a
          typed reason.
        </p>
      )}
      {err && <p className="mb-4 text-caption-md st-danger">[x] {err}</p>}
      {loading && <p className="text-caption-md text-mute">[~] loading…</p>}
      <div className="space-y-3">
        {models.map((m) => (
          <ModelCard
            key={m.name}
            model={m}
            overrideVersion={overrideTarget?.name === m.name ? overrideTarget.version : null}
            onChanged={load}
          />
        ))}
        {!loading && models.length === 0 && (
          <p className="text-body-md text-mute">[ ] no registered models.</p>
        )}
      </div>
    </>
  );
}

function ModelCard({
  model,
  overrideVersion,
  onChanged,
}: {
  model: ModelRow;
  overrideVersion: string | null;
  onChanged: () => void;
}) {
  // A card carrying the override target opens itself — the hand-off should land ready to review.
  const [open, setOpen] = useState(overrideVersion !== null);
  const [detail, setDetail] = useState<ModelDetail | null>(null);
  const [err, setErr] = useState('');

  const loadDetail = useCallback(async () => {
    try {
      setDetail(await gwGet<ModelDetail>(`models/${encodeURIComponent(model.name)}`));
    } catch (e) {
      setErr(String(e));
    }
  }, [model.name]);

  useEffect(() => {
    if (open && !detail) loadDetail();
  }, [open, detail, loadDetail]);

  const toggle = () => setOpen(!open);

  return (
    <Panel>
      <button onClick={toggle} className="flex w-full items-center justify-between text-left">
        <span className="text-body-strong text-ink">
          <span className="st-mute">[{open ? '−' : '+'}]</span> {model.name}
        </span>
        {model.serving_version ? (
          <Badge tone="accent">champion @v{model.serving_version}</Badge>
        ) : (
          <span className="text-caption-md text-ash">[ ] none promoted</span>
        )}
      </button>

      {err && <p className="mt-2 text-caption-md st-danger">[x] {err}</p>}

      {open && detail && (
        <>
          <PromoteGate
            name={model.name}
            versions={detail.versions}
            championVersion={detail.serving?.version ?? null}
            overrideVersion={overrideVersion}
            onChanged={() => {
              loadDetail();
              onChanged();
            }}
          />
          <div className="mt-4">
            <EvaluatePanel
              name={model.name}
              versions={detail.versions}
              championVersion={detail.serving?.version ?? null}
            />
          </div>
        </>
      )}
    </Panel>
  );
}
