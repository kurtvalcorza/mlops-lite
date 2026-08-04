'use client';

// 027 US1 (T704/T705) — the Overview: the landing view answers four questions in order.
//
//   1. Is the platform healthy?      2. What is running right now?
//   3. What needs attention?         4. What has been happening?
//
// The ordering is the design. An operator opening the console is either checking on something or
// reacting to something, and both start with "is anything wrong" — so health leads, attention comes
// before history, and the areas list is last because it is only useful once the first three say
// nothing is on fire.
//
// Every panel degrades to "unknown" rather than to a plausible-looking zero, and every one shows its
// own data age, because these questions are answered by different backends with different staleness.

import Link from 'next/link';
import { Panel, PageTitle } from '@/components/Panel';
import { AREAS } from '@/lib/areas';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type {
  ActivityEvent,
  AdmissionView,
  AttentionView,
  Capabilities,
  PlatformHealth,
  PlatformJob,
  RuntimeHost,
  SummaryCards,
} from '@/lib/platform-types';

/** `null` renders as the word "unknown". Never `0` — see `SummaryCards`. */
function Value({ value, suffix }: { value: number | null | undefined; suffix?: string }) {
  if (value === null || value === undefined) return <span className="text-ash">unknown</span>;
  return (
    <span className="text-ink">
      {value}
      {suffix}
    </span>
  );
}

function Age({ ageMs, degraded }: { ageMs: number | null; degraded: string[] }) {
  return (
    <span className="text-caption-md text-ash">
      {formatAge(ageMs)}
      {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
    </span>
  );
}

// -- 1. is the platform healthy? ------------------------------------------------------------------

const STATE_MARK: Record<string, string> = {
  healthy: 'ok',
  degraded: ' !',
  critical: '!!',
  unknown: ' ?',
};

/**
 * FR-369/370. The aggregate and the per-service panel, with `overall` and `mode` kept distinct:
 * `overall` is how well the platform is working, `mode` is what kind of deployment it is. Both are
 * server-resolved, so the interface never has to decide that a missing field means "fine".
 */
function Health() {
  const { data, ageMs, degraded } = useLive<PlatformHealth>('console/health', CADENCE.health);
  return (
    <Panel title={`Platform — ${data?.overall ?? 'unknown'}`}>
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
          {/* One row per service, each labelled with whether its loss is a degradation or a genuine
              stop — the difference FR-370 exists to preserve. */}
          <ul className="mt-2 font-mono text-body-md">
            {data.services.map((service) => (
              <li
                key={service.service}
                className={service.state === 'healthy' ? 'text-mute' : 'text-ink'}
              >
                [{STATE_MARK[service.state] ?? ' ?'}] {service.service}
                {service.state !== 'healthy' && (
                  <span className="text-ash">
                    {' '}
                    —{' '}
                    {service.required
                      ? 'required: training and inference cannot proceed'
                      : 'optional: degrades'}
                  </span>
                )}
                {service.detail && <span className="text-ash"> · {service.detail}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

// -- 2. what is running right now? ----------------------------------------------------------------

/** The eight summary cards (FR-371). Values come from the server so `null` cannot become `0` here. */
function Cards() {
  const { data, ageMs, degraded } = useLive<SummaryCards>('console/summary', CADENCE.jobs);
  return (
    <Panel title="Summary">
      <div className="mb-2">
        <Age ageMs={ageMs} degraded={degraded} />
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-body-md sm:grid-cols-4">
        <Card label="endpoints" value={data?.activeEndpoints} />
        <Card label="running jobs" value={data?.runningJobs} />
        <Card label="gpu util" value={data?.gpuUtilization} suffix="%" />
        {/* NOT "pending admissions". Admission decides synchronously and has no queue, so that card
            would read 0 forever — which reads as "requests never wait" and is the opposite of what
            a refusal means. */}
        <div>
          <dt className="text-ash">admissions</dt>
          <dd>
            {data?.admissionDecisions ? (
              <span className="text-ink">
                {data.admissionDecisions.refused} refused / {data.admissionDecisions.admitted} admitted
              </span>
            ) : (
              <span className="text-ash">unknown</span>
            )}
          </dd>
        </div>
        <Card label="failed jobs" value={data?.failedJobs} />
        <Card label="models to review" value={data?.modelsRequiringReview} />
        <Card label="unlabeled" value={data?.unlabeledCaptures} />
        <Card label="drift warnings" value={data?.driftWarnings} />
      </dl>
    </Panel>
  );
}

function Card({
  label,
  value,
  suffix,
}: {
  label: string;
  value: number | null | undefined;
  suffix?: string;
}) {
  return (
    <div>
      <dt className="text-ash">{label}</dt>
      <dd>
        <Value value={value} suffix={suffix} />
      </dd>
    </div>
  );
}

/** The unified active-work table (FR-372) — one row shape across three sources. */
function ActiveWork() {
  const { data, ageMs, degraded } = useLive<PlatformJob[]>('console/jobs?limit=25', CADENCE.jobs);
  const admission = useLive<AdmissionView>('runtime/admission', CADENCE.runtime);

  return (
    <Panel title="Active work">
      <div className="mb-2">
        <Age ageMs={ageMs} degraded={degraded} />
      </div>
      {data === null ? (
        // Not "nothing is running". No source answered, so no claim is made.
        <p className="text-body-md text-mute">unknown — no job source answered</p>
      ) : data.length === 0 ? (
        <p className="text-body-md text-mute">nothing in flight, based on what is readable</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-body-md">
            <thead className="text-ash">
              <tr>
                <th className="pr-4 text-left">job</th>
                <th className="pr-4 text-left">state</th>
                <th className="pr-4 text-left">gateway</th>
                <th className="pr-4 text-left">agent</th>
                <th className="text-left">tracking</th>
              </tr>
            </thead>
            <tbody>
              {data.map((job) => (
                <tr key={job.id} className={job.conflict?.conflict ? 'text-ink' : 'text-mute'}>
                  <td className="pr-4">
                    <Link href={`/training/jobs/${job.id}`} className="underline">
                      {job.id}
                    </Link>
                  </td>
                  <td className="pr-4 text-ink">{job.normalizedState}</td>
                  {/* All three natives shown alongside the normalized state (FR-392): the
                      normalization is for scanning, the natives are for debugging. */}
                  <td className="pr-4">{job.gatewayState ?? '—'}</td>
                  <td className="pr-4">{job.agentState ?? '—'}</td>
                  <td>{job.trackingRunState ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {admission.data?.active_job && (
        <p className="mt-2 text-body-md text-ink">
          Exclusive job {admission.data.active_job.job_id} holds the whole GPU. A running job is
          never preempted.
        </p>
      )}
    </Panel>
  );
}

// -- 3. what needs attention? ---------------------------------------------------------------------

const SEVERITY_MARK: Record<string, string> = { critical: '!!', warning: ' !', info: '  ' };

function Attention() {
  const { data, ageMs, degraded } = useLive<AttentionView>('console/attention', CADENCE.health);

  return (
    <Panel title="Needs attention">
      <div className="mb-2">
        <Age ageMs={ageMs} degraded={degraded} />
      </div>
      {data === null ? (
        <p className="text-body-md text-mute">unknown — no source answered</p>
      ) : data.items.length === 0 && degraded.length > 0 ? (
        // Distinct from "nothing needs attention". An unreachable backend cannot tell us there is
        // nothing wrong, and saying so would be the console's most dangerous falsehood.
        <p className="text-body-md text-mute">
          nothing from the sources that answered — {degraded.join(', ')} could not be read
        </p>
      ) : data.items.length === 0 ? (
        <p className="text-body-md text-mute">nothing, based on what is currently readable</p>
      ) : (
        <ul className="font-mono text-body-md">
          {data.items.map((item) => (
            <li key={item.id} className={item.severity === 'info' ? 'text-mute' : 'text-ink'}>
              [{SEVERITY_MARK[item.severity] ?? '  '}] {item.kind} · {item.subject} —{' '}
              <Link href={item.href} className="underline">
                {item.detail}
              </Link>
            </li>
          ))}
        </ul>
      )}
      {data && data.kindsNotPolled.length > 0 && (
        // Which checks this panel did NOT run. A kind that cannot fire is indistinguishable from a
        // kind that found nothing wrong, and "nothing needs attention" is the most consequential
        // sentence on this page.
        <p className="mt-3 text-caption-md text-ash">
          [i] not checked here: {data.kindsNotPolled.join(', ')} — both need per-version work too
          expensive to poll, and are computed on demand in{' '}
          <Link href="/models" className="underline">
            Models
          </Link>
          .
        </p>
      )}
    </Panel>
  );
}

// -- 4. what has been happening? ------------------------------------------------------------------

/**
 * The lifecycle timeline (FR-363). 021 navigated by loop stage; the loop lives on here as a
 * *visualization*, because how work moves through the platform is still true — it just no longer
 * decides where an operator clicks.
 */
function Activity() {
  const { data, ageMs, degraded } = useLive<ActivityEvent[]>(
    'console/activity?limit=25',
    CADENCE.jobs,
  );

  return (
    <Panel title="Recent activity">
      <div className="mb-2">
        <Age ageMs={ageMs} degraded={degraded} />
      </div>
      {data === null ? (
        <p className="text-body-md text-mute">unknown — no source answered</p>
      ) : data.length === 0 ? (
        <p className="text-body-md text-mute">nothing recorded in the readable sources</p>
      ) : (
        <ul className="font-mono text-body-md">
          {data.map((event, i) => (
            <li key={`${event.kind}-${event.subject}-${i}`} className="text-mute">
              <span className="text-ash">[{event.stage}]</span>{' '}
              <Link href={event.href} className="underline text-ink">
                {event.subject}
              </Link>{' '}
              {event.kind}
              {event.detail && ` · ${event.detail}`}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// -- where to go next -----------------------------------------------------------------------------

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

/** Kept from the first cut: the host row still answers "is the GPU box there at all". */
function Hosts() {
  const { data } = useLive<RuntimeHost[]>('runtime/hosts', CADENCE.runtime);
  if (!data) return null;
  return (
    <p className="font-mono text-caption-md text-ash">
      {data.map((h) => `${h.host}: ${h.reachable ? 'reachable' : 'unreachable'} · ${h.active_engines.length} engine(s)`).join(' · ')}
    </p>
  );
}

export default function OverviewPage() {
  return (
    <div>
      <PageTitle sub="Health, what is running, what needs attention, and what has been happening">
        Overview
      </PageTitle>
      <div className="flex flex-col gap-6">
        <Health />
        <Hosts />
        <Cards />
        <ActiveWork />
        <Attention />
        <Activity />
        <WhatNext />
      </div>
    </div>
  );
}
