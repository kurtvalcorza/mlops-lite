'use client';

// 027 US1 — the Overview: the landing view answers four questions in order.
//
//   1. Is the platform healthy?      2. What is running right now?
//   3. What needs attention?         4. What would I do next?
//
// The ordering is the design. An operator opening the console is either checking on something or
// reacting to something, and both start with "is anything wrong" — so health leads, attention comes
// before navigation, and the "what next" links are last because they are only useful once the first
// three say nothing is on fire.
//
// Every panel degrades to "unknown" rather than to a plausible-looking zero, and every one shows its
// own data age, because the four questions are answered by four different backends with four
// different staleness.

import Link from 'next/link';
import { Panel, PageTitle } from '@/components/Panel';
import { AREAS } from '@/lib/areas';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type {
  AdmissionView,
  Capabilities,
  PlatformHealth,
  RuntimeHost,
} from '@/lib/platform-types';

function Age({ ageMs, degraded }: { ageMs: number | null; degraded: string[] }) {
  return (
    <span className="text-caption-md text-ash">
      {formatAge(ageMs)}
      {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
    </span>
  );
}

/** 1 — is the platform healthy? */
function Health() {
  const { data, ageMs, degraded } = useLive<PlatformHealth>('console/health', CADENCE.health);
  return (
    <Panel title="Platform">
      <div className="mb-2">
        <Age ageMs={ageMs} degraded={degraded} />
      </div>
      {!data ? (
        <p className="text-body-md text-mute">
          The gateway is not answering. Nothing is claimed about the platform&apos;s state.
        </p>
      ) : (
        <>
          <p className="text-body-md text-ink">
            mode <span className="font-mono">{data.mode}</span>
          </p>
          <ul className="mt-2 font-mono text-body-md">
            {Object.entries(data.services).map(([name, up]) => (
              <li key={name} className={up ? 'text-mute' : 'text-ink'}>
                [{up ? 'ok' : '!!'}] {name}
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

/** 2 — what is running right now? */
function Running() {
  const hosts = useLive<RuntimeHost[]>('runtime/hosts', CADENCE.runtime);
  const admission = useLive<AdmissionView>('runtime/admission', CADENCE.runtime);
  const resident = admission.data?.residents ?? null;

  return (
    <Panel title="Running now">
      <div className="mb-2">
        <Age ageMs={hosts.ageMs} degraded={hosts.degraded} />
      </div>
      {!hosts.data ? (
        <p className="text-body-md text-mute">unknown — the host agent is not reporting</p>
      ) : (
        <>
          <p className="font-mono text-body-md text-ink">
            {hosts.data[0]?.jobs_active ?? 'unknown'} job(s) ·{' '}
            {hosts.data[0]?.active_engines.length ?? 'unknown'} engine(s) up
          </p>
          {resident === null ? (
            <p className="mt-2 text-body-md text-mute">resident models unknown</p>
          ) : resident.length === 0 ? (
            <p className="mt-2 text-body-md text-mute">nothing resident — models load on demand</p>
          ) : (
            <ul className="mt-2 font-mono text-body-md">
              {resident.map((r) => (
                <li key={r.model_key}>
                  {r.model_key} · {r.vram_gb} GB · {r.state} · {r.active_requests} in flight
                </li>
              ))}
            </ul>
          )}
          {admission.data?.active_job && (
            <p className="mt-2 text-body-md text-ink">
              Exclusive job {admission.data.active_job.job_id} holds the whole GPU. It is never
              preempted.
            </p>
          )}
        </>
      )}
    </Panel>
  );
}

/** 3 — what needs attention? Derived, never invented: each item names its evidence. */
function Attention() {
  const health = useLive<PlatformHealth>('console/health', CADENCE.health);
  const hosts = useLive<RuntimeHost[]>('runtime/hosts', CADENCE.runtime);
  const admission = useLive<AdmissionView>('runtime/admission', CADENCE.runtime);

  const items: { text: string; href: string }[] = [];

  for (const [name, up] of Object.entries(health.data?.services ?? {})) {
    if (!up) items.push({ text: `${name} is unreachable`, href: '/observability/health' });
  }
  if (hosts.data?.[0]?.wedged) {
    items.push({ text: 'an engine is wedged', href: '/runtime' });
  }
  if ((hosts.data?.[0]?.interrupted_since_start ?? 0) > 0) {
    items.push({
      text: `${hosts.data?.[0]?.interrupted_since_start} job(s) interrupted by a restart`,
      href: '/runtime',
    });
  }
  const refusals = (admission.data?.records ?? []).filter((r) => r.decision === 'refused');
  if (refusals.length > 0) {
    items.push({
      text: `${refusals.length} recent admission refusal(s) — ${refusals[0].reason ?? 'see detail'}`,
      href: '/runtime',
    });
  }

  const known = health.data !== null || hosts.data !== null;

  return (
    <Panel title="Needs attention">
      {!known ? (
        // Distinct from "nothing needs attention". An unreachable backend cannot tell us there is
        // nothing wrong, and saying so would be the console's most dangerous falsehood.
        <p className="text-body-md text-mute">
          unknown — not enough of the platform is reachable to say
        </p>
      ) : items.length === 0 ? (
        <p className="text-body-md text-mute">nothing, based on what is currently readable</p>
      ) : (
        <ul className="text-body-md">
          {items.map((item) => (
            <li key={item.text}>
              <Link href={item.href} className="underline">
                {item.text}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/** 4 — what would I do next? Areas whose controls this deployment actually supports. */
function WhatNext() {
  const { data } = useLive<Capabilities>('console/capabilities', CADENCE.admin);
  // An unsupported area is OMITTED rather than shown and made to fail (FR-418/433).
  const hidden = new Set<string>();
  if (data && !data.broker) hidden.add('administration');
  if (data && !data.runtime_reads) hidden.add('runtime');

  return (
    <Panel title="Areas">
      <ul className="text-body-md">
        {AREAS.filter((a) => a.slug !== 'overview' && !hidden.has(a.slug)).map((area) => (
          <li key={area.slug}>
            <Link href={'/' + area.slug} className="underline">
              {area.label}
            </Link>{' '}
            <span className="text-mute">— {area.description}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export default function OverviewPage() {
  return (
    <div>
      <PageTitle sub="Health, what is running, what needs attention, and where to go next">
        Overview
      </PageTitle>
      <div className="flex flex-col gap-6">
        <Health />
        <Running />
        <Attention />
        <WhatNext />
      </div>
    </div>
  );
}
