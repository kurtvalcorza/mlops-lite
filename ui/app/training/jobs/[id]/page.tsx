'use client';

// 027 US4 (T728) — one job, across its three identifiers (FR-391/393/394).
//
// This page is the point of the increment for anyone debugging a fine-tune. The same unit of work
// has a gateway job id, a host-agent job id, and a tracking run id, and until now answering "what
// is it doing" meant holding all three and querying three systems in three vocabularies. Here it is
// one row, one timeline, one resource panel — with all three native states preserved, because the
// normalization is for scanning and the natives are what you actually debug with.
//
// Logs reuse the existing `/runs/{id}/events` SSE stream. No new streaming surface (research R10):
// a second one would be a second thing to keep alive, and the existing one already works.

import { useEffect, useRef, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { ConflictBanner } from '@/components/ConflictBanner';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { PlatformJob } from '@/lib/platform-types';

export default function JobPage() {
  const routeParams = useParams<{ id: string }>();
  const id = decodeURIComponent(String(routeParams.id));
  const { data, ageMs, degraded } = useLive<PlatformJob>(
    `console/jobs/${encodeURIComponent(id)}`,
    CADENCE.jobs,
  );

  return (
    <>
      <PageTitle sub="The gateway record, the agent execution, and the tracking run — as one view.">
        job {id}
      </PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/training" className="underline text-mute">
          ← training
        </Link>
      </p>

      <ConflictBanner conflict={data?.conflict} />

      <div className="flex flex-col gap-6">
        <Panel title="State">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(ageMs)}
            {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
          </p>
          {!data ? (
            <p className="text-body-md text-mute">unknown — no source answered for this job</p>
          ) : (
            <dl className="font-mono text-body-md">
              <Row label="normalized" value={data.normalizedState} strong />
              {/* All three natives, always. This is the half of SC-190 that a normalization layer
                  is most tempted to drop. */}
              <Row label="gateway says" value={data.gatewayState} />
              <Row label="agent says" value={data.agentState} />
              <Row label="tracking says" value={data.trackingRunState} />
              <Row label="type" value={data.type} />
              {data.admissionReason && <Row label="admission" value={data.admissionReason} strong />}
            </dl>
          )}
        </Panel>

        <Panel title="Timeline">
          {!data?.timeline || data.timeline.length === 0 ? (
            <p className="text-body-md text-mute">no transitions recorded</p>
          ) : (
            <ul className="font-mono text-body-md">
              {data.timeline.map((entry) => (
                <li key={entry.event}>
                  <span className="text-ash">{new Date(entry.at * 1000).toISOString()}</span>{' '}
                  {entry.event}
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Resources">
          <dl className="font-mono text-body-md">
            <Row label="host" value={data?.resources?.host ?? data?.assignedHost ?? null} />
            <Row
              label="device"
              value={
                data?.resources?.device_index === null || data?.resources?.device_index === undefined
                  ? null
                  : String(data.resources.device_index)
              }
            />
            <Row
              label="vram"
              value={data?.resources?.vram_gb ? `${data.resources.vram_gb} GB` : null}
            />
          </dl>
        </Panel>

        <Panel title="Cross-links">
          {/* SC-189: gateway record, agent execution, and tracking run reachable in one
              interaction each. */}
          <ul className="text-body-md">
            <li>
              <Link href="/runtime#journal" className="underline">
                agent journal
              </Link>{' '}
              <span className="text-mute">— what the agent actually did</span>
            </li>
            {data?.runId && (
              <li>
                <Link href={`/training/runs/${data.runId}`} className="underline">
                  tracking run {data.runId.slice(0, 8)}
                </Link>
              </li>
            )}
            {data?.studyId && (
              <li>
                <Link href={`/training/studies/${data.studyId}`} className="underline">
                  study {data.studyId}
                </Link>
              </li>
            )}
            {data?.modelId && (
              <li>
                <Link href={`/models/${encodeURIComponent(data.modelId)}`} className="underline">
                  model {data.modelId}
                </Link>
              </li>
            )}
          </ul>
        </Panel>

        {data?.runId && <Logs runId={data.runId} />}
      </div>
    </>
  );
}

function Row({
  label,
  value,
  strong,
}: {
  label: string;
  value: string | null | undefined;
  strong?: boolean;
}) {
  return (
    <div className="flex gap-3">
      <dt className="w-32 shrink-0 text-ash">{label}</dt>
      <dd className={strong ? 'text-ink' : 'text-mute'}>{value ?? 'unknown'}</dd>
    </div>
  );
}

/**
 * The existing run-event stream, reused (research R10).
 *
 * An interrupted stream is REPORTED, never silently truncated (FR-395). A log view that just stops
 * is indistinguishable from a job that went quiet, and those call for opposite reactions.
 */
function Logs({ runId }: { runId: string }) {
  const [lines, setLines] = useState<string[]>([]);
  const [interrupted, setInterrupted] = useState(false);
  const box = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    const source = new EventSource(`/api/gw/runs/${encodeURIComponent(runId)}/events`);
    source.onmessage = (event) => {
      // Bounded retention: the last 500 lines. An unbounded log buffer in a page an operator leaves
      // open all day is the most likely cause of a console footprint regression (SC-199).
      setLines((prev) => [...prev, event.data].slice(-500));
    };
    source.onerror = () => {
      setInterrupted(true);
      source.close();
    };
    return () => source.close();
  }, [runId]);

  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);

  return (
    <Panel title="Logs">
      <pre
        ref={box}
        className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-caption-md text-mute"
      >
        {lines.join('\n') || 'no events yet'}
      </pre>
      {interrupted && (
        <p className="mt-2 text-caption-md st-warning">
          [!] the log stream was interrupted — this is not the end of the job&apos;s output
        </p>
      )}
    </Panel>
  );
}
