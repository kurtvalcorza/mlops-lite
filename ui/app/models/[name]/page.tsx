'use client';

// 027 US3 — one model's versions, with 021's promote gate kept intact.
//
// The gate did not move because 027 reorganized navigation; it moved because it belongs next to the
// versions it acts on. Nothing about it changed: preview → promote, override with a typed reason,
// evaluate on demand, and the `?override=<name>@<version>` deep-link from the retraining inbox.
// 027 adds no write surface (US11 owns that) and removes none.

import { Suspense, useCallback, useEffect, useState } from 'react';
import { useParams, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { Badge } from '@/components/Badge';
import { PageTitle, Panel } from '@/components/Panel';
import { EvaluatePanel } from '@/components/models/EvaluatePanel';
import { PromoteGate, type Version } from '@/components/models/PromoteGate';
import { gwGet } from '@/lib/gw';

type ModelDetail = { name: string; serving: { version: string } | null; versions: Version[] };

export default function ModelPage() {
  return (
    <Suspense fallback={<p className="text-caption-md text-ash">[~] loading…</p>}>
      <ModelView />
    </Suspense>
  );
}

function ModelView() {
  const routeParams = useParams<{ name: string }>();
  const name = decodeURIComponent(String(routeParams.name));
  const params = useSearchParams();

  const [detail, setDetail] = useState<ModelDetail | null>(null);
  const [err, setErr] = useState('');

  // retraining → models hand-off: a blocked candidate arrives here to review for override.
  const overrideRaw = params.get('override') ?? '';
  const overrideVersion =
    overrideRaw.includes('@') && overrideRaw.slice(0, overrideRaw.lastIndexOf('@')) === name
      ? overrideRaw.slice(overrideRaw.lastIndexOf('@') + 1)
      : null;

  const load = useCallback(async () => {
    try {
      setDetail(await gwGet<ModelDetail>(`models/${encodeURIComponent(name)}`));
      setErr('');
    } catch (e) {
      setErr(String(e));
    }
  }, [name]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <PageTitle sub="Versions, the promote gate, and evaluation on demand.">{name}</PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/models" className="underline text-mute">
          ← catalog
        </Link>
      </p>

      {overrideVersion && (
        <p className="mb-4 text-caption-md st-warning">
          [!] override review: <span className="text-ink">v{overrideVersion}</span> arrived
          gate-blocked from retraining — overriding requires a typed reason.
        </p>
      )}
      {err && <p className="mb-4 text-caption-md st-danger">[x] {err}</p>}
      {!detail ? (
        <p className="text-caption-md text-mute">[~] loading…</p>
      ) : (
        <Panel>
          <p className="mb-3">
            {detail.serving ? (
              <Badge tone="accent">champion @v{detail.serving.version}</Badge>
            ) : (
              <span className="text-caption-md text-ash">[ ] none promoted</span>
            )}
          </p>
          <ul className="mb-4 font-mono text-body-md">
            {detail.versions.map((v) => (
              <li key={v.version}>
                <Link
                  href={`/models/${encodeURIComponent(name)}/${v.version}`}
                  className="underline text-ink"
                >
                  v{v.version}
                </Link>
              </li>
            ))}
          </ul>
          <PromoteGate
            name={name}
            versions={detail.versions}
            championVersion={detail.serving?.version ?? null}
            overrideVersion={overrideVersion}
            onChanged={load}
          />
          <div className="mt-4">
            <EvaluatePanel
              name={name}
              versions={detail.versions}
              championVersion={detail.serving?.version ?? null}
            />
          </div>
        </Panel>
      )}
    </>
  );
}
