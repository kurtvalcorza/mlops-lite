'use client';

import { useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CycleBoard, type CycleBoardHandle } from '@/components/retraining/CycleBoard';
import { PolicyEditor, type PolicyDoc } from '@/components/retraining/PolicyEditor';
import { SuggestionsInbox } from '@/components/retraining/SuggestionsInbox';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type { DriftView } from '@/lib/platform-types';

// 021 T444 (FR-243..248): the retraining stage — the previously-invisible autonomous layer made
// visible: declare standing per-model policies (form+JSON), watch the cycle board, and work the
// suggestions inbox. The reciprocal manual-vs-standing note frames the relationship with
// /monitoring (FR-248): SAME checks, SAME gate, SAME shared cooldown — different trigger.
export default function RetrainingPage() {
  const boardRef = useRef<CycleBoardHandle>(null);
  const [editing, setEditing] = useState<{ model_name: string; doc: PolicyDoc } | null>(null);

  return (
    <>
      <PageTitle sub="Standing per-model policies close the loop on their own; suggestions keep you in charge.">
        retraining
      </PageTitle>

      {/* 027 T739 (FR-406): the statistic's limitations, stated ON the surface. Not a tooltip and
          not a docs link — every number below is subject to all four, and an operator reading a
          0.31 without them may conclude the model has degraded when the input distribution simply
          moved. */}
      <DriftLimits />

      {/* FR-248 (reciprocal of the monitoring note): standing vs manual, same machinery */}
      <p className="mb-6 text-caption-md text-mute">
        [i] policies here run the <span className="text-ink">same</span> monitoring checks on a{' '}
        <span className="text-ink">standing schedule</span>. The manual, one-shot counterpart lives
        in{' '}
        <Link href="/monitoring" className="st-accent underline">
          monitoring
        </Link>{' '}
        — same gate, same cooldown; only the trigger differs.
      </p>

      <div className="mb-6 grid gap-6 lg:grid-cols-[1fr_1.4fr]">
        <PolicyEditor
          key={editing ? `edit:${editing.model_name}` : 'new'}
          initial={editing}
          onSaved={() => {
            setEditing(null);
            boardRef.current?.refresh();
          }}
        />
        <CycleBoard ref={boardRef} onEdit={setEditing} />
      </div>

      <SuggestionsInbox onPromoted={() => boardRef.current?.refresh()} />
    </>
  );
}

/**
 * 027 T738/T739 — the drift reports with their thresholds, and the surface's stated limitations.
 *
 * The thresholds come **from the payload** (FR-405). Nothing here hard-codes 0.10 or 0.25, so
 * tuning the convention in configuration cannot leave this page quietly disagreeing with the
 * backend about what counts as drift.
 *
 * The limitations text is a static property of the surface (FR-406) and is rendered unconditionally
 * — including when no report has drifted. A caveat that appears only alongside a bad number reads
 * as an excuse for that number rather than as a property of the method.
 */
function DriftLimits() {
  const { data, ageMs, degraded } = useLive<DriftView>('console/drift', CADENCE.catalog);

  return (
    <Panel title="Drift reports">
      <p className="mb-2 text-caption-md text-ash">
        {formatAge(ageMs)}
        {degraded.length > 0 && ` · unreachable: ${degraded.join(', ')}`}
      </p>

      {data === null ? (
        <p className="text-body-md text-mute">unknown — the report store did not answer</p>
      ) : data.reports.length === 0 ? (
        <p className="text-body-md text-mute">no reports recorded</p>
      ) : (
        <ul className="font-mono text-body-md text-mute">
          {data.reports.map((report, i) => (
            <li key={`${report.modelName}-${i}`}>
              {report.modelName ?? 'unknown model'} · max{' '}
              {report.maxStatistic === null ? (
                // Not `0`. An empty feature list cannot support a "no drift" claim.
                <span className="text-ash">not measured</span>
              ) : (
                <span className="text-ink">{report.maxStatistic.toFixed(3)}</span>
              )}{' '}
              <span className="text-ash">
                (warning ≥ {report.thresholds.warning}, significant ≥ {report.thresholds.significant})
              </span>{' '}
              · {report.featureCount} feature(s)
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 text-caption-md text-ash">
        <p className="text-mute">[i] what this statistic does and does not tell you:</p>
        <ul>
          {(data?.limitations ?? STATIC_LIMITATIONS).map((line) => (
            <li key={line}>· {line}</li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}

/**
 * Shown when the reports themselves are unreadable. The limitations are a property of the METHOD,
 * not of any particular response, so an unreachable backend must not be able to remove them — a
 * page that dropped its caveats during an outage would be at its least reliable exactly when it
 * looked most confident.
 */
const STATIC_LIMITATIONS = [
  'This detects distributional change between two windows. It does not prove model degradation.',
  'It does not establish causality — a shifted input distribution may be entirely benign.',
  'The result depends on the chosen baseline window; a different baseline gives a different answer.',
  'The result depends on binning; PSI is computed over reference deciles.',
];
