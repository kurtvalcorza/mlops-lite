'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

// The six operator surfaces. primary-nav from the source = ASCII wordmark + links.
const TABS = [
  { href: '/infer', label: 'infer' },
  { href: '/models', label: 'models' },
  { href: '/datasets', label: 'datasets' },
  { href: '/runs', label: 'runs' },
  { href: '/monitor', label: 'monitor' },
  { href: '/health', label: 'health' },
];

export function Nav() {
  const path = usePathname();
  return (
    <header className="hairline border-x-0 border-t-0">
      <div className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center gap-x-6 gap-y-2 px-6 py-4">
        <Link href="/infer" className="text-heading-md tracking-tight text-ink">
          MLOPS-LITE
        </Link>
        <span className="text-caption-md text-ash">// operator console</span>
        <nav className="ml-auto flex flex-wrap items-center gap-1">
          {TABS.map((t) => {
            const active = path === t.href || path.startsWith(t.href + '/');
            return (
              <Link
                key={t.href}
                href={t.href}
                className={
                  'rounded-sm px-3 py-1 text-button-md ' +
                  (active
                    ? 'bg-card text-ink'
                    : 'text-mute hover:bg-soft hover:text-ink')
                }
              >
                <span className={active ? 'st-accent' : 'st-mute'}>[{active ? '*' : ' '}]</span>{' '}
                {t.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
