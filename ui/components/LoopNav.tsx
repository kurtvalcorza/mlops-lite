'use client';

// 021 T425 (FR-208/209/212, research R1): the nav IS the loop. Six stages rendered in lifecycle
// order with directional connectors and a loop-back marker returning to `data`; `health` and the
// GPU pill sit OFF the loop axis (right-aligned). Each stage carries a live StageBadge glyph fed
// by the single useLiveState mount below. Replaces the flat noun-list Nav.tsx.
//
// Responsive (T456): the off-axis chrome wraps below the loop bar at narrow widths; the six
// ordered stages stay on one axis so the loop is never visually broken.

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { GpuPill } from '@/components/GpuPill';
import { StageBadge, type StageKey } from '@/components/StageBadge';
import { useLiveState } from '@/lib/useLiveState';

const STAGES: { key: StageKey; href: string }[] = [
  { key: 'data', href: '/data' },
  { key: 'training', href: '/training' },
  { key: 'models', href: '/models' },
  { key: 'serving', href: '/serving' },
  { key: 'monitoring', href: '/monitoring' },
  { key: 'retraining', href: '/retraining' },
];

export function LoopNav() {
  const path = usePathname();
  const live = useLiveState(); // ONE live-state mount for the whole shell (badges + pill)

  const healthActive = path === '/health' || path.startsWith('/health/');

  return (
    <header className="hairline border-x-0 border-t-0">
      <div className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center gap-x-6 gap-y-1 px-6 pt-3">
        <Link href="/serving" className="text-heading-md tracking-tight text-ink">
          MLOPS-LITE
        </Link>
        <span className="text-caption-md text-ash">// operator console</span>
        {/* off-axis chrome: GPU pill + health — right-aligned, not part of the ordered loop */}
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <GpuPill live={live} />
          <Link
            href="/health"
            className={
              'rounded-sm px-2 py-0.5 text-caption-md ' +
              (healthActive ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
            }
          >
            [{healthActive ? '*' : ' '}] health
          </Link>
        </span>
      </div>
      {/* the loop axis — six ordered stages, directional connectors, loop-back marker */}
      <nav
        aria-label="lifecycle loop"
        className="mx-auto flex w-full max-w-[1100px] flex-nowrap items-center gap-1 overflow-x-auto px-6 pb-2 pt-1"
      >
        {STAGES.map((s, i) => {
          const active = path === s.href || path.startsWith(s.href + '/');
          return (
            <span key={s.key} className="flex items-center gap-1 whitespace-nowrap">
              {i > 0 && (
                <span aria-hidden className="text-ash">
                  →
                </span>
              )}
              <Link
                href={s.href}
                className={
                  'flex items-baseline gap-1 rounded-sm px-2 py-0.5 text-button-md ' +
                  (active ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
                }
              >
                <span>{s.key}</span>
                <StageBadge stage={s.key} live={live} />
              </Link>
            </span>
          );
        })}
        {/* loop-back: the last stage feeds the first — the loop, not a line */}
        <span aria-hidden className="ml-1 text-ash" title="retraining feeds back into data — the loop closes">
          ⟲
        </span>
      </nav>
    </header>
  );
}
