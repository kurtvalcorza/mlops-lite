'use client';

// 027 T697 — the ten-area shell nav, replacing 021's LoopNav.
//
// Why a sidebar-style list rather than the loop bar: the loop bar encoded *order* with directional
// connectors, and ten areas of concern have no order to encode. Rendering them on the same axis
// would keep the arrows while making them mean nothing, which is worse than dropping them — an
// interface element that implies a relationship the system does not have is the same class of error
// as a "queue" view over an admission path that never queues.
//
// The GPU pill and the mode badge sit off-axis, as they did before.

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { GpuPill } from '@/components/GpuPill';
import { AREAS } from '@/lib/areas';
import { useLiveState } from '@/lib/useLiveState';
import { CADENCE, useLive } from '@/lib/use-live';
import type { PlatformHealth } from '@/lib/platform-types';

/**
 * 027 T733 (FR-429) — the persistent mode badge, plus the aggregate health state.
 *
 * Two different facts, shown side by side because they answer different questions and conflating
 * them was a real mistake made and corrected during this increment. **Mode** is what kind of
 * deployment this is (`offline` fixtures / `live` services / `hardware` GPU) — an operator reading
 * a GPU number needs to know whether there is a GPU behind it. **Health** is how well that
 * deployment is currently working. A fixture-backed console can be perfectly healthy and still must
 * not be mistaken for the hardware one.
 *
 * Both are resolved from reachability by the server, never self-declared (research R14).
 */
function ModeBadge() {
  const { data } = useLive<PlatformHealth>('console/health', CADENCE.health);
  if (!data) {
    // Unknown, not "live". A badge that defaults to a working state is the falsehood this exists
    // to prevent — it is the first thing on screen and the last thing anyone re-checks.
    return <span className="text-caption-md text-ash">mode unknown</span>;
  }
  const tone =
    data.overall === 'healthy'
      ? 'text-mute'
      : data.overall === 'degraded'
        ? 'text-ink'
        : 'text-red-700';
  return (
    <span className={`text-caption-md ${tone}`}>
      {data.mode} · {data.overall}
    </span>
  );
}

export function AreaNav() {
  const path = usePathname();
  const live = useLiveState();

  return (
    <header className="hairline border-x-0 border-t-0">
      <div className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center gap-x-6 gap-y-1 px-6 pt-3">
        <Link href="/overview" className="text-heading-md tracking-tight text-ink">
          MLOPS-LITE
        </Link>
        {/* the literal "//" is DISPLAY text (the console wordmark), not a comment */}
        <span className="text-caption-md text-ash">{'// operator console'}</span>
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <ModeBadge />
          <GpuPill live={live} />
        </span>
      </div>

      <nav
        aria-label="console areas"
        className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center gap-1 px-6 pb-2 pt-1"
      >
        {AREAS.map((area) => {
          const href = '/' + area.slug;
          const active = path === href || path.startsWith(href + '/');
          return (
            <Link
              key={area.slug}
              href={href}
              title={area.description}
              className={
                'whitespace-nowrap rounded-sm px-2 py-0.5 text-caption-md ' +
                (active ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
              }
            >
              [{active ? '*' : ' '}] {area.label.toLowerCase()}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
