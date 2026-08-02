'use client';

// 027 US6 (T746) — the inference record: predictions, captures, and the review queue.
//
// **Payloads are hidden because they are never sent**, not because they are styled away. The list
// and detail projections carry a `PayloadPreview` with no content at all; revealing is an explicit
// second call, and it is a POST so no payload reference ever lands in a URL — URLs reach access
// logs, browser history, and `Referer` headers, permanently, for every reveal anyone performs.
//
// The panels that SEND requests live in Deployments, next to what is serving. This area is the
// record of what was already sent.

import { useState } from 'react';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { CaptureRow, PredictionRecord, ReviewItem } from '@/lib/platform-types';

const TABS = ['Predictions', 'Captures', 'Review queue'] as const;
type Tab = (typeof TABS)[number];

export default function InferencePage() {
  const [tab, setTab] = useState<Tab>('Predictions');

  return (
    <div>
      <PageTitle sub="What was served, what was captured, and what needs a label">
        Inference
      </PageTitle>

      <nav className="mb-4 flex flex-wrap gap-1 text-caption-md" aria-label="inference views">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={
              'rounded-sm px-2 py-0.5 ' +
              (t === tab ? 'bg-card text-ink' : 'text-mute hover:bg-soft hover:text-ink')
            }
          >
            [{t === tab ? '*' : ' '}] {t.toLowerCase()}
          </button>
        ))}
      </nav>

      {tab === 'Predictions' && <Predictions />}
      {tab === 'Captures' && <Captures />}
      {tab === 'Review queue' && <ReviewQueue />}
    </div>
  );
}

function Predictions() {
  const { data, ageMs, degraded } = useLive<PredictionRecord[]>(
    'console/predictions?limit=50',
    CADENCE.jobs,
  );

  return (
    <Panel title="Predictions">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>
      {data === null ? (
        <p className="text-body-md text-mute">unknown — the prediction record did not answer</p>
      ) : data.length === 0 ? (
        <p className="text-body-md text-mute">nothing served in the readable window</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-body-md">
            <thead className="text-ash">
              <tr>
                <th className="pr-4 text-left">prediction</th>
                <th className="pr-4 text-left">modality</th>
                <th className="pr-4 text-left">model</th>
                <th className="pr-4 text-left">ver</th>
                <th className="pr-4 text-left">capture</th>
                <th className="text-left">label</th>
              </tr>
            </thead>
            <tbody>
              {data.map((record) => (
                <tr key={record.id} className="text-mute">
                  <td className="pr-4">
                    <Link
                      href={`/inference/predictions/${encodeURIComponent(record.id)}`}
                      className="text-ink underline"
                    >
                      {record.id.slice(0, 12)}
                    </Link>
                  </td>
                  <td className="pr-4">{record.modality}</td>
                  <td className="pr-4">{record.modelName ?? '—'}</td>
                  <td className="pr-4">{record.registryVersion ?? '—'}</td>
                  <td className="pr-4">{record.captureState}</td>
                  <td>{record.labelState}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Captures() {
  const { data, ageMs, degraded } = useLive<CaptureRow[]>('console/captures', CADENCE.jobs);
  return (
    <Panel title="Captures">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>
      {data === null ? (
        <p className="text-body-md text-mute">unknown — the capture index did not answer</p>
      ) : data.length === 0 ? (
        <p className="text-body-md text-mute">nothing captured</p>
      ) : (
        <ul className="font-mono text-body-md text-mute">
          {data.map((row) => (
            <li key={row.predictionId}>
              <Link
                href={`/inference/predictions/${encodeURIComponent(row.predictionId)}`}
                className="text-ink underline"
              >
                {row.predictionId.slice(0, 12)}
              </Link>{' '}
              {row.modality} · {row.labelState}
              {/* The capture's object reference is NOT a link. Bytes move only through the
                  explicit reveal on the detail page. */}
              {row.hasPayload && <span className="text-ash"> · payload stored</span>}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ReviewQueue() {
  const { data, ageMs, degraded } = useLive<ReviewItem[]>('console/review-queue', CADENCE.jobs);
  return (
    <Panel title="Review queue">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>
      {data === null ? (
        <p className="text-body-md text-mute">unknown — the capture index did not answer</p>
      ) : data.length === 0 ? (
        <p className="text-body-md text-mute">nothing waiting</p>
      ) : (
        <ul className="font-mono text-body-md text-mute">
          {data.map((item) => (
            <li key={item.predictionId}>
              <Link
                href={`/inference/predictions/${encodeURIComponent(item.predictionId)}`}
                className="text-ink underline"
              >
                {item.predictionId.slice(0, 12)}
              </Link>{' '}
              {/* Every item says WHICH signal put it here. A queue that ranks without saying why
                  is one an operator has to take on faith. */}
              <span className="text-ink">{item.reason}</span>
              {item.signals.length > 1 && (
                <span className="text-ash"> (+{item.signals.slice(1).join(', ')})</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
