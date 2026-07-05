'use client';

// 021 T445 (FR-224/225): lineage drill-back for one registered version — run_id → training (the
// run detail is polled there via ?run=), dataset tag → data, base/parent rendered inline. A
// version with NO run_id is seeded/imported: visually distinct, no fabricated lineage.

import Link from 'next/link';

export type VersionTags = Record<string, string>;

export function LineageLinks({ runId, tags }: { runId?: string | null; tags: VersionTags }) {
  const dataset = tags?.dataset ?? tags?.dataset_version ?? null; // "name@version"
  const base = tags?.base_model ?? null;
  const parent = tags?.parent_version ?? tags?.parent ?? null;

  if (!runId) {
    return (
      <p className="text-caption-md text-ash">
        <span className="st-mute">[◇]</span> seeded / imported — no training run recorded
        {base && <> · base {base}</>}
      </p>
    );
  }

  return (
    <p className="text-caption-md text-ash">
      <span className="st-mute">[†]</span>{' '}
      <Link
        href={`/training?run=${encodeURIComponent(runId)}`}
        className="underline"
        title="drill back to the training run"
      >
        run {runId.length > 14 ? `${runId.slice(0, 14)}…` : runId}
      </Link>
      {dataset && (
        <>
          {' '}
          ·{' '}
          <Link href="/data" className="underline" title={`trained on ${dataset}`}>
            {dataset}
          </Link>
        </>
      )}
      {base && <> · base {base}</>}
      {parent && <> · chained from v{parent}</>}
    </p>
  );
}
