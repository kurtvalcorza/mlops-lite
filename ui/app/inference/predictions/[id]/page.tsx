'use client';

// 027 US6 (T746) — one prediction, its payload (only on request), and its trace.
//
// This page is where the payload rule becomes visible. The record arrives with a `PayloadPreview`
// that has **no `preview` key at all** — not empty, absent. There is nothing on this page to hide,
// because there is nothing here to render until the operator asks. Asking is a `POST` with the
// identifier in the body, so the request that reveals a payload leaves no trace of *which* payload
// in any URL, log, history entry, or referrer.
//
// The trace waterfall makes **no token-oriented assumptions** (FR-413). This platform serves five
// modalities: an image classification has no tokens and an embedding call has no completion, so the
// waterfall shows duration and nesting — which every modality has — and leaves anything
// token-shaped in the span's own attributes.

import { useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { SpanWaterfall } from '@/lib/charts';
import { gwPost } from '@/lib/gw';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { Envelope, PayloadPreview, PredictionDetail, TraceDetail } from '@/lib/platform-types';

export default function PredictionPage() {
  const routeParams = useParams<{ id: string }>();
  const id = decodeURIComponent(String(routeParams.id));
  const { data, ageMs, degraded } = useLive<PredictionDetail>(
    `console/predictions/${encodeURIComponent(id)}`,
    CADENCE.jobs,
  );

  return (
    <>
      <PageTitle sub="The record, its payload on request, and the spans behind it.">
        prediction {id.slice(0, 12)}
      </PageTitle>
      <p className="mb-3 text-caption-md">
        <Link href="/inference" className="underline text-mute">
          ← inference
        </Link>
      </p>

      <div className="flex flex-col gap-6">
        <Panel title="Record">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(ageMs)}
            {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
          </p>
          {!data ? (
            <p className="text-body-md text-mute">unknown — no record for this id</p>
          ) : (
            <dl className="font-mono text-body-md">
              <Row label="served" value={data.timestamp ? String(data.timestamp) : null} />
              <Row label="modality" value={data.modality} />
              <Row label="model" value={data.modelName} />
              <Row label="version" value={data.registryVersion} />
              <Row label="status" value={data.status} />
              <Row label="capture" value={data.captureState} />
              <Row label="label" value={data.labelState} />
              <Row label="policy" value={data.policyResult} />
            </dl>
          )}
        </Panel>

        {data && <Payload id={id} preview={data.payload} />}
        {data?.traceId && <Trace traceId={data.traceId} />}
      </div>
    </>
  );
}

function Row({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="flex gap-3">
      <dt className="w-32 shrink-0 text-ash">{label}</dt>
      <dd className="text-ink">{value ?? 'unknown'}</dd>
    </div>
  );
}

function Payload({ id, preview }: { id: string; preview: PayloadPreview }) {
  const [revealed, setRevealed] = useState<PayloadPreview | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function reveal() {
    setBusy(true);
    try {
      // The identifier travels in the BODY. The path segment exists only so the route reads
      // conventionally; putting the id in a query string here would undo the whole point.
      const body = await gwPost<Envelope<PayloadPreview>>(
        `console/predictions/${encodeURIComponent(id)}/payload`,
        { prediction_id: id },
      );
      setRevealed(body.data);
      setError('');
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Payload">
      {!preview.available ? (
        <p className="text-body-md text-mute">no payload was captured for this prediction</p>
      ) : !revealed?.preview ? (
        <>
          <p className="text-body-md text-mute">
            Hidden. The content was not sent with this page — revealing it is a separate request.
            {preview.totalBytes !== null && ` ${preview.totalBytes} bytes stored.`}
          </p>
          <button
            onClick={reveal}
            disabled={busy}
            className="mt-2 rounded-sm bg-card px-2 py-0.5 text-caption-md text-ink"
          >
            {busy ? '[~] revealing…' : '[ ] reveal payload'}
          </button>
          {error && <p className="mt-2 text-caption-md st-danger">[x] {error}</p>}
        </>
      ) : (
        <>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-caption-md text-mute">
            {revealed.preview}
          </pre>
          {revealed.truncated && (
            // The TRUE size, not the truncated one: an operator deciding whether to pull the object
            // needs to know how big it actually is.
            <p className="mt-2 text-caption-md st-warning">
              [!] truncated for display — the stored payload is {revealed.totalBytes} bytes
            </p>
          )}
          {revealed.redactedFields.length > 0 && (
            <p className="mt-1 text-caption-md text-ash">
              redacted: {revealed.redactedFields.join(', ')}
            </p>
          )}
        </>
      )}
    </Panel>
  );
}

function Trace({ traceId }: { traceId: string }) {
  const { data } = useLive<TraceDetail>(
    `console/traces/${encodeURIComponent(traceId)}`,
    CADENCE.jobs,
  );

  return (
    <Panel title="Trace">
      {!data ? (
        <p className="text-body-md text-mute">unknown — the tracking server did not answer</p>
      ) : data.spans.length === 0 ? (
        <p className="text-body-md text-mute">no spans recorded</p>
      ) : (
        <>
          <p className="mb-2 font-mono text-caption-md text-ash">
            {data.spans.length} span(s) · {data.totalDurationMs} ms
          </p>
          <div className="overflow-x-auto text-ink">
            <SpanWaterfall
              spans={data.spans.map((span) => ({
                name: span.name,
                startMs: span.startMs,
                durationMs: span.durationMs,
                depth: span.depth ?? 0,
              }))}
              label="span waterfall"
            />
          </div>
          <ul className="mt-2 font-mono text-caption-md text-mute">
            {data.spans.map((span) => (
              <li key={span.spanId} className={span.status === 'error' ? 'text-ink' : ''}>
                {'  '.repeat(span.depth ?? 0)}
                {span.name} · {span.durationMs} ms
                {span.status === 'error' && ' · ERROR'}
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}
