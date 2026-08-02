'use client';

// 026 T656/T671 — the broker console: who is on the GPU, what each bound has left, who is waiting,
// and what each tenant has spent.
//
// The page is deliberately read-first. The one thing an operator needs from it during a drill is to
// be able to *check the two VRAM invariants by reading numbers*, not by inferring them — an earlier
// revision of the API exposed only free VRAM, so neither bound was checkable from the documented
// surface and a mid-transition observation looked like a violation. Both terms of both bounds are
// therefore shown as terms, with the comparison spelled out.

import { useEffect, useState } from 'react';
import { Panel, PageTitle } from '@/components/Panel';
import { gwGet } from '@/lib/gw';

type Resident = {
  model: string;
  state: string;
  vram_mb: number;
  active_requests: number;
  idle: boolean;
};

type Reservation = {
  op_id: string;
  model: string;
  est_mb: number;
  materialized: boolean;
  waiters: number;
};

type QueueView = {
  resident: Resident[];
  reservations: Reservation[];
  vram: {
    usable_capacity_mb: number;
    accounted_mb: number;
    reserved_mb: number;
    unmaterialized_mb: number;
    live_free_mb: number;
    safety_headroom_mb: number;
  };
  inference_lane: { drain_mode?: boolean; admissions_since_job_queued?: number };
  jobs_lane: { job_id: string; tenant_id?: string; pos: number; kind?: string }[];
  active_job: { job_id: string; started_at: number } | null;
  job_barrier: boolean;
};

type UsageRow = {
  tenant: string;
  tenant_id: string;
  window: string | null;
  budget_gpu_seconds: number | null;
  consumed_gpu_seconds: number | null;
  settled_gpu_seconds: number | null;
  outstanding_gpu_seconds: number | null;
  remaining_gpu_seconds: number | null;
};

type UsageView = {
  per_tenant: UsageRow[];
  total_gpu_seconds: number;
  ledger: { id: number; tenant_id: string; kind: string; ref_id: string; gpu_seconds: number }[];
  reconciliation: { ledger_total: number; settled_reservation_total: number; reconciled: boolean };
};

/** Poll a gateway read. `null` means "not known right now" — never a stale value presented as live. */
function usePoll<T>(path: string, intervalMs: number): T | null {
  const [value, setValue] = useState<T | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<T>(path)
        .then((v) => alive && setValue(v))
        .catch(() => alive && setValue(null));
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [path, intervalMs]);
  return value;
}

const gb = (mb: number | undefined) => ((mb ?? 0) / 1024).toFixed(2);
const secs = (s: number | null | undefined) => (s == null ? '—' : s.toFixed(1));

/** The two bounds, each shown as its terms plus the comparison, so a drill reads rather than infers. */
function VramBounds({ vram }: { vram: QueueView['vram'] }) {
  const accountedTotal = vram.accounted_mb + vram.reserved_mb;
  const bound1 = accountedTotal <= vram.usable_capacity_mb;
  const headroomLeft = vram.live_free_mb - vram.unmaterialized_mb - vram.safety_headroom_mb;

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div className="hairline p-3">
        <div className="text-caption-md text-ash">
          bound 1 — accounted set within the budget
        </div>
        <div className="mt-1 font-mono text-body-md text-ink">
          {gb(vram.accounted_mb)} resident + {gb(vram.reserved_mb)} reserved ={' '}
          {gb(accountedTotal)} GB {bound1 ? '≤' : '>'} {gb(vram.usable_capacity_mb)} GB usable
        </div>
        <div className={`mt-1 text-caption-md ${bound1 ? 'text-mute' : 'text-red-700'}`}>
          {bound1 ? 'holds' : 'VIOLATED'}
        </div>
      </div>
      <div className="hairline p-3">
        <div className="text-caption-md text-ash">
          bound 2 — room for the next load, against live free
        </div>
        <div className="mt-1 font-mono text-body-md text-ink">
          {gb(vram.live_free_mb)} free − {gb(vram.unmaterialized_mb)} unmaterialized −{' '}
          {gb(vram.safety_headroom_mb)} headroom = {gb(headroomLeft)} GB
        </div>
        <div className="mt-1 text-caption-md text-mute">
          the largest model that could be admitted right now
        </div>
      </div>
    </div>
  );
}

export default function BrokerPage() {
  const queue = usePoll<QueueView>('admin/queue', 3000);
  const usage = usePoll<UsageView>('admin/usage', 8000);

  return (
    <main className="mx-auto max-w-5xl px-6 py-8">
      <PageTitle sub="LAN self-service GPU broker — residency, lanes, quotas, and the usage ledger">
        Broker
      </PageTitle>

      <div className="flex flex-col gap-6">
        <Panel
          title="GPU residency"
          hint={queue ? `${queue.resident.length} resident` : 'unreachable'}
        >
          {!queue ? (
            <p className="text-body-md text-mute">
              The host agent is not reporting. This is unknown, not empty — nothing is claimed about
              what is on the GPU.
            </p>
          ) : (
            <>
              <VramBounds vram={queue.vram} />

              {queue.job_barrier && (
                <p className="mt-4 text-body-md text-ink">
                  A job drain is in progress: serving admission is closed while residents finish
                  their in-flight requests. Nothing is being preempted.
                </p>
              )}
              {queue.active_job && (
                <p className="mt-4 text-body-md text-ink">
                  Exclusive job <span className="font-mono">{queue.active_job.job_id}</span> holds
                  the whole GPU. It is never preempted.
                </p>
              )}

              <table className="mt-4 w-full text-body-md">
                <thead className="text-caption-md text-ash">
                  <tr>
                    <th className="py-1 text-left">model</th>
                    <th className="text-left">state</th>
                    <th className="text-right">VRAM</th>
                    <th className="text-right">in flight</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {queue.resident.map((r) => (
                    <tr key={r.model} className="hairline border-x-0 border-b-0">
                      <td className="py-1">{r.model}</td>
                      {/* `state` matters: a loading or draining resident is otherwise
                          indistinguishable from a settled one, which is what makes a
                          mid-transition read look like a broken invariant. */}
                      <td>{r.state}</td>
                      <td className="text-right">{gb(r.vram_mb)} GB</td>
                      <td className="text-right">{r.active_requests}</td>
                    </tr>
                  ))}
                  {queue.resident.length === 0 && (
                    <tr>
                      <td colSpan={4} className="py-2 text-mute">
                        nothing resident — models load on demand
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>

              {queue.reservations.length > 0 && (
                <div className="mt-4">
                  <div className="text-caption-md text-ash">outstanding reservations</div>
                  <ul className="mt-1 font-mono text-body-md">
                    {queue.reservations.map((r) => (
                      <li key={r.op_id}>
                        {r.model} · {gb(r.est_mb)} GB ·{' '}
                        {r.materialized ? 'measured' : 'not yet materialized'}
                        {r.waiters > 0 && ` · ${r.waiters} waiting`}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </Panel>

        <Panel
          title="Queue"
          hint={queue ? `${queue.jobs_lane.length} job(s) waiting` : undefined}
        >
          {!queue ? (
            <p className="text-body-md text-mute">unreachable</p>
          ) : (
            <>
              {queue.inference_lane?.drain_mode && (
                <p className="mb-3 text-body-md text-ink">
                  Job-drain mode: new inference is refused so the head job can acquire the GPU.
                  Requests already running finish normally.
                </p>
              )}
              {queue.jobs_lane.length === 0 ? (
                <p className="text-body-md text-mute">the jobs lane is empty</p>
              ) : (
                <ol className="font-mono text-body-md">
                  {queue.jobs_lane.map((j) => (
                    <li key={j.job_id}>
                      {j.pos}. {j.job_id}
                      {j.kind ? ` · ${j.kind}` : ''}
                    </li>
                  ))}
                </ol>
              )}
            </>
          )}
        </Panel>

        <Panel
          title="Tenants and quotas"
          hint={usage ? `${secs(usage.total_gpu_seconds)} GPU-seconds this window` : undefined}
        >
          {!usage ? (
            <p className="text-body-md text-mute">unreachable</p>
          ) : (
            <>
              <table className="w-full text-body-md">
                <thead className="text-caption-md text-ash">
                  <tr>
                    <th className="py-1 text-left">tenant</th>
                    <th className="text-left">window</th>
                    <th className="text-right">consumed</th>
                    <th className="text-right">in flight</th>
                    <th className="text-right">remaining</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {usage.per_tenant.map((t) => (
                    <tr key={t.tenant_id} className="hairline border-x-0 border-b-0">
                      <td className="py-1">{t.tenant}</td>
                      <td>{t.window ?? 'unmetered'}</td>
                      {/* `consumed` counts settled rows AND outstanding reservations — the number
                          the quota is actually enforced against. Showing only the settled half
                          would display a tenant well inside its budget at the moment the broker
                          refuses it. */}
                      <td className="text-right">{secs(t.consumed_gpu_seconds)}</td>
                      <td className="text-right">{secs(t.outstanding_gpu_seconds)}</td>
                      <td className="text-right">{secs(t.remaining_gpu_seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-3 text-caption-md text-ash">
                GPU-seconds is the canonical unit; &ldquo;credits&rdquo; is only a display alias.
                Ledger totals{' '}
                {usage.reconciliation?.reconciled ? 'reconcile' : 'DO NOT reconcile'} with settled
                reservations ({secs(usage.reconciliation?.ledger_total)} vs{' '}
                {secs(usage.reconciliation?.settled_reservation_total)}).
              </p>
            </>
          )}
        </Panel>
      </div>
    </main>
  );
}
