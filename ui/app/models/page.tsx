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

  const promote = async (version: string) => {
    setBusy(version);
    setErr('');
    try {
      await gwPost(`models/${encodeURIComponent(model.name)}/promote`, { version });
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

      {open && detail && (
        <ul className="mt-3 divide-y divide-hairline">
          {detail.versions.map((v) => (
            <li key={v.version} className="flex items-center justify-between gap-3 py-2">
              <span className="text-body-md text-ink">
                <span className={v.serving ? 'st-accent' : 'st-mute'}>
                  [{v.serving ? '✓' : ' '}]
                </span>{' '}
                v{v.version}
                {v.tags?.kind && <span className="ml-2 text-caption-md text-ash">{v.tags.kind}</span>}
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
          ))}
        </ul>
      )}
    </Panel>
  );
}
