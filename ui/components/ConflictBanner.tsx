'use client';

// 027 T732 (FR-427) — the state-conflict banner.
//
// Two systems of record disagreeing about one entity is **disclosed, never resolved by precedence**.
// Picking a winner would hide the fact that the platform is internally inconsistent, which is the
// single thing an operator most needs to know and the one thing no amount of downstream polish can
// recover once the interface has silently chosen.
//
// So the banner shows every source's answer, with the time each was observed, and offers actions
// rather than a verdict:
//
//   * **refresh** — the honest first move when the readings might simply be stale.
//   * **inspect-journal** — the agent's journal is the durable record of what actually happened,
//     and it is where a real disagreement gets settled.
//   * **reconcile** — surfaced and INERT in 027. MVP 3 owns automated reconciliation; rendering a
//     working button here would be the decorative-control failure FR-418 exists to forbid, and
//     rendering nothing would hide that reconciliation is the eventual answer.
//
// A conflict whose observations are further apart than the skew threshold is not shown as a
// conflict at all — the server suppresses the claim, and this component renders the suppression
// explicitly rather than silently drawing nothing. A stale read disagreeing with a fresh one is not
// evidence of inconsistency, and a banner that cried wolf would cost the real conflicts their
// audience.

import Link from 'next/link';
import type { JobConflict } from '@/lib/platform-types';

export function ConflictBanner({ conflict }: { conflict: JobConflict | null | undefined }) {
  if (!conflict) return null;

  if (conflict.skewExceeded) {
    return (
      <p className="hairline mb-3 p-2 text-caption-md text-ash">
        [~] The sources disagree, but their readings are too far apart in time to compare — no
        conflict is claimed. Refresh to compare readings taken together.
      </p>
    );
  }

  if (!conflict.conflict) return null;

  return (
    <div className="hairline mb-3 p-2">
      <p className="text-body-md text-ink">
        [!] Sources disagree about {conflict.entity}{' '}
        <span className="font-mono">{conflict.entityId}</span>. Neither answer is preferred here.
      </p>
      <ul className="mt-1 font-mono text-caption-md text-mute">
        {conflict.sources.map((source) => (
          <li key={source.source}>
            {source.source}: {source.state ?? 'no record'}
            <span className="text-ash"> · observed {source.observedAt ?? 'unknown'}</span>
          </li>
        ))}
      </ul>
      {conflict.lastConsistentAt && (
        <p className="mt-1 text-caption-md text-ash">last consistent {conflict.lastConsistentAt}</p>
      )}
      <p className="mt-2 text-caption-md">
        <Link href="/runtime#journal" className="underline text-ink">
          inspect journal
        </Link>
        {' · '}
        <span className="text-ash" title="MVP 3 owns automated reconciliation; inert in 027">
          reconcile (not available in this release)
        </span>
      </p>
    </div>
  );
}
