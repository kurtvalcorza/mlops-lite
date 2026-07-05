'use client';

// 021 T427 (FR-211): the persistent GPU-lease pill — always visible in the header, off the loop
// axis. Shows lease holder + resident model + swap/idle state from the shared LiveState
// (serving/state + platform/events); click-through lands on /serving (the full LeaseView).
// Principle II visualize-only: this surface READS the lease, it never touches it.

import Link from 'next/link';
import type { LiveState } from '@/lib/useLiveState';

export function GpuPill({ live }: { live: LiveState }) {
  const { lease, reachable } = live;

  let glyph: React.ReactNode;
  let label: string;
  let title: string;

  if (reachable === false || reachable === null) {
    glyph = <span className="text-ash">?</span>;
    label = 'unreachable';
    title = 'platform unreachable — lease state unknown';
  } else if (lease === null) {
    glyph = <span className="text-ash">?</span>;
    label = 'unknown';
    title = 'lease state unknown';
  } else if (lease.resident) {
    const model = lease.serving_model
      ? `${lease.serving_model}${lease.serving_version ? `@v${lease.serving_version}` : ''}`
      : 'model';
    glyph = <span className="st-accent">●</span>;
    label = `${lease.holder ?? 'serving'} · ${model}`;
    title = `GPU lease held by ${lease.holder ?? 'serving'} — ${model} resident (click for the lease view)`;
  } else {
    glyph = <span className="st-mute">○</span>;
    label = 'idle';
    title = 'GPU lease idle — nothing resident (click for the lease view)';
  }

  return (
    <Link
      href="/serving"
      title={title}
      className="hairline flex max-w-[260px] items-center gap-2 rounded-full bg-soft px-3 py-0.5 text-caption-md text-ink hover:bg-card"
    >
      <span className="text-mute">GPU</span>
      {glyph}
      <span className="truncate">{label}</span>
    </Link>
  );
}
