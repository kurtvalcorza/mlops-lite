'use client';

// 027 T715/T716 — the Runtime area: hosts, per-device topology, engine processes, admission
// decisions, and the journal.
//
// Two rules govern this page and neither is cosmetic:
//
//   * **No control that would preempt a running job** (FR-379). There are no buttons here at all.
//     Refusal is presented as designed behaviour — the explanation says a running job is never
//     preempted — rather than as a problem the operator is invited to override.
//   * **The interface never composes its own admission wording** (FR-378). `explanation` is rendered
//     verbatim from the server, which composed it from the same values the decision used. A
//     client-side sentence would drift from admission's real reasoning the first time either
//     changed.
//
// Fallback-derived values are labelled from the `source` field rather than guessed, and a null is
// rendered as "unknown" — never as a zero an operator would act on.

import { Panel, PageTitle } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type {
  AdmissionRecord,
  AdmissionView,
  EngineProcess,
  JournalPage,
  RuntimeDevice,
  RuntimeHost,
} from '@/lib/platform-types';

/** `null` is unknown and says so. Rendering it as 0 would be a false statement, not a terse one. */
function n(value: number | null | undefined, unit = ''): string {
  return value === null || value === undefined ? 'unknown' : `${value}${unit}`;
}

function Age({ ageMs, stale, degraded }: { ageMs: number | null; stale: boolean; degraded: string[] }) {
  return (
    <span className="text-caption-md text-ash">
      {formatAge(ageMs)}
      {stale && ' · stale'}
      {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
    </span>
  );
}

function SourceBadge({ source }: { source: string }) {
  // Provenance is data, not inference: `static` means the GPU could not be read at all, and every
  // per-device number on that path is null rather than a plausible-looking default.
  const label =
    source === 'nvml' ? 'NVML' : source === 'smi' ? 'nvidia-smi (fallback)' : 'unreadable (static)';
  return <span className="text-caption-md text-ash">{label}</span>;
}

function Hosts() {
  const { data, ageMs, stale, degraded } = useLive<RuntimeHost[]>(
    'runtime/hosts',
    CADENCE.runtime,
  );
  return (
    <Panel title="Hosts" hint={data ? `${data.length} host(s)` : undefined}>
      <div className="mb-2">
        <Age ageMs={ageMs} stale={stale} degraded={degraded} />
      </div>
      {!data ? (
        <p className="text-body-md text-mute">
          The host agent is not reporting. This is unknown, not empty — no claim is made about what
          is running.
        </p>
      ) : (
        <table className="w-full text-body-md">
          <thead className="text-caption-md text-ash">
            <tr>
              <th className="py-1 text-left">host</th>
              <th className="text-left">engines up</th>
              <th className="text-right">devices</th>
              <th className="text-right">jobs</th>
              <th className="text-right">free VRAM</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {data.map((host) => (
              <tr key={host.host} className="hairline border-x-0 border-b-0">
                <td className="py-1">{host.host}</td>
                <td>{host.active_engines.join(', ') || '—'}</td>
                <td className="text-right">{n(host.device_count)}</td>
                <td className="text-right">{n(host.jobs_active)}</td>
                <td className="text-right">{n(host.gpu_free_gb, ' GB')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function Devices() {
  const { data, ageMs, stale, degraded } = useLive<{
    observed_at: string;
    source: string;
    devices: RuntimeDevice[];
  }>('runtime/hosts/local/devices', CADENCE.runtime);

  return (
    <Panel title="Devices" hint={data ? `${data.devices.length} device(s)` : undefined}>
      <div className="mb-2 flex items-baseline gap-3">
        <Age ageMs={ageMs} stale={stale} degraded={degraded} />
        {data && <SourceBadge source={data.source} />}
      </div>
      {!data ? (
        <p className="text-body-md text-mute">unknown — the agent is not reporting</p>
      ) : (
        data.devices.map((device) => (
          <div key={device.index} className="hairline mb-3 p-3">
            <div className="font-mono text-body-md text-ink">
              [{device.index}] {device.name ?? 'unknown device'}
            </div>
            <div className="mt-1 font-mono text-body-md text-mute">
              {n(device.used_vram_gb, ' GB')} used / {n(device.total_vram_gb, ' GB')} total ·{' '}
              {n(device.free_vram_gb, ' GB')} free · {n(device.utilization_pct, '%')} util ·{' '}
              {n(device.temperature_c, '°C')}
            </div>
            {device.processes.length > 0 && (
              <ul className="mt-2 font-mono text-caption-md text-ash">
                {device.processes.map((proc) => (
                  <li key={proc.pid}>
                    pid {proc.pid} · {n(proc.vram_gb, ' GB')} ·{' '}
                    {proc.engine_id ?? 'unattributed'}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))
      )}
    </Panel>
  );
}

function Engines() {
  const { data, ageMs, stale, degraded } = useLive<{ engines: EngineProcess[] }>(
    'runtime/engines',
    CADENCE.runtime,
  );
  const engines = data?.engines ?? null;

  return (
    <Panel title="Engine processes" hint={engines ? `${engines.length}` : undefined}>
      <div className="mb-2">
        <Age ageMs={ageMs} stale={stale} degraded={degraded} />
      </div>
      {!engines ? (
        <p className="text-body-md text-mute">unknown — the agent is not reporting</p>
      ) : (
        <table className="w-full text-body-md">
          <thead className="text-caption-md text-ash">
            <tr>
              <th className="py-1 text-left">engine</th>
              <th className="text-left">process</th>
              <th className="text-left">residency</th>
              <th className="text-left">loaded identity</th>
              <th className="text-right">VRAM</th>
              <th className="text-right">in flight</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {engines.map((engine) => (
              <tr key={engine.engine_id} className="hairline border-x-0 border-b-0">
                <td className="py-1">{engine.engine_id}</td>
                {/* Process health and residency are two different facts. Collapsing them would lose
                    "the child is fine but its model is being evicted". */}
                <td>{engine.state}</td>
                <td>{engine.residency_state ?? '—'}</td>
                {/* The AGENT-reported loaded identity, never the registry's desired pointer — the
                    two legitimately diverge during an activation, which is when it matters. */}
                <td>{engine.model_identity ?? 'unknown'}</td>
                <td className="text-right">{n(engine.vram_gb, ' GB')}</td>
                <td className="text-right">{n(engine.active_requests)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function Bounds({ view }: { view: AdmissionView }) {
  // Both checks, each with ITS OWN reservation term. Never collapsed: `live_free_gb` already
  // excludes current residents, so summing the resident set against it double-counts them — the
  // v1.6.0 defect v1.6.1 corrected, and reproducing it here would misreport why a model was refused.
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <div className="hairline p-3">
        <div className="text-caption-md text-ash">budget check — bounds the accounted set</div>
        <div className="mt-1 font-mono text-body-md text-ink">
          {n(view.accounted_resident_gb)} resident + {n(view.reserved_gb)} reserved ≤{' '}
          {n(view.usable_budget_gb, ' GB')} usable
        </div>
      </div>
      <div className="hairline p-3">
        <div className="text-caption-md text-ash">live-VRAM check — bounds the incoming load</div>
        <div className="mt-1 font-mono text-body-md text-ink">
          {n(view.live_free_gb)} free − {n(view.unmaterialized_gb)} unmaterialized −{' '}
          {n(view.headroom_gb)} headroom
        </div>
      </div>
    </div>
  );
}

function Decision({ record }: { record: AdmissionRecord }) {
  return (
    <li className="hairline border-x-0 border-t-0 py-2">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-caption-md text-ash">{record.decided_at}</span>
        <span className="text-caption-md text-ash">
          {record.decision}
          {record.reason ? ` · ${record.reason}` : ''}
        </span>
      </div>
      {/* Rendered VERBATIM. The interface must not compose its own wording, or it drifts from
          admission's real reasoning. */}
      <p className="mt-1 text-body-md text-ink">{record.explanation}</p>
      {record.residents.length > 0 && (
        <p className="mt-1 font-mono text-caption-md text-ash">
          resident at decision time:{' '}
          {record.residents
            .map((r) => `${r.model_key} (${r.vram_gb} GB, ${r.state}, ${r.active_requests} in flight)`)
            .join(' · ')}
        </p>
      )}
    </li>
  );
}

function Admission() {
  const { data, ageMs, stale, degraded } = useLive<AdmissionView>(
    'runtime/admission',
    CADENCE.runtime,
  );

  return (
    <Panel
      title="Admission decisions"
      hint={data ? `${data.records.length} of ${data.capacity} kept` : undefined}
    >
      <div className="mb-2">
        <Age ageMs={ageMs} stale={stale} degraded={degraded} />
      </div>
      {!data ? (
        <p className="text-body-md text-mute">unknown — the agent is not reporting</p>
      ) : (
        <>
          <Bounds view={data} />
          {data.job_barrier && (
            <p className="mt-3 text-body-md text-ink">
              A job drain is in progress: serving admission is closed while residents finish their
              in-flight requests. Nothing is being preempted.
            </p>
          )}
          {/* This is a decision HISTORY, not a queue — admission decides immediately, so there is
              no pending state to show and no position to report. */}
          <ul className="mt-3">
            {data.records.length === 0 ? (
              <li className="text-body-md text-mute">no decisions recorded yet</li>
            ) : (
              data.records.map((record) => <Decision key={record.id} record={record} />)
            )}
          </ul>
        </>
      )}
    </Panel>
  );
}

function Journal() {
  const { data, ageMs, stale, degraded } = useLive<JournalPage>(
    'runtime/journal?limit=25',
    CADENCE.jobs,
  );

  return (
    <Panel title="Journal" hint={data?.has_more ? 'more available' : undefined}>
      <div className="mb-2">
        <Age ageMs={ageMs} stale={stale} degraded={degraded} />
      </div>
      {!data ? (
        <p className="text-body-md text-mute">unknown — the agent is not reporting</p>
      ) : data.entries.length === 0 ? (
        <p className="text-body-md text-mute">no entries</p>
      ) : (
        <ul className="font-mono text-body-md">
          {data.entries.map((entry) => (
            <li key={entry.sequence} className="hairline border-x-0 border-b-0 py-1">
              <span className="text-ash">{entry.timestamp ?? 'unknown time'}</span>{' '}
              {entry.job_id ?? '—'} → {entry.to_state ?? '—'}
              {/* A torn tail is SHOWN as torn. A missing final transition is exactly what an
                  operator investigating a crash needs to see. */}
              {entry.checksum_state === 'torn' && (
                <span className="text-ink"> · torn (no final transition recorded)</span>
              )}
              {entry.detail && <span className="text-ash"> · {entry.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

export default function RuntimePage() {
  return (
    <main className="mx-auto max-w-5xl px-6 py-8">
      <PageTitle sub="Devices, engine processes, admission decisions, and the durable journal">
        Runtime
      </PageTitle>
      <div className="flex flex-col gap-6">
        <Hosts />
        <Devices />
        <Engines />
        <Admission />
        <Journal />
      </div>
    </main>
  );
}
