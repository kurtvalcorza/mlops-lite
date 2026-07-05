'use client';

import { Suspense, useRef } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/Panel';
import { DriftPanel } from '@/components/monitoring/DriftPanel';
import { HistoryList, type HistoryHandle } from '@/components/monitoring/HistoryList';
import { LabelsPanel } from '@/components/monitoring/LabelsPanel';
import { QualityPanel } from '@/components/monitoring/QualityPanel';

// 021 T439 (FR-238..242/248): the monitoring stage — BOTH breach signals (input drift + output
// quality), both report histories, ground-truth labeling, and the one-shot retrain arm with
// cooldown as a first-class outcome. The standing policy loop lives in /retraining (US4); the
// in-page note below states the manual-vs-standing relationship explicitly (FR-248).
export default function MonitoringPage() {
  const historyRef = useRef<HistoryHandle>(null);
  const refreshHistory = () => historyRef.current?.refresh();

  return (
    <>
      <PageTitle sub="Watch what serving does to quality: run checks, read histories, attach ground truth.">
        monitoring
      </PageTitle>

      {/* FR-248: these are MANUAL, ONE-SHOT checks; the standing counterpart is declared in retraining */}
      <p className="mb-6 text-caption-md text-mute">
        [i] checks here are <span className="text-ink">manual and one-shot</span>. Their{' '}
        <span className="text-ink">standing, scheduled</span> counterpart is a per-model policy in{' '}
        <Link href="/retraining" className="st-accent underline">
          retraining
        </Link>{' '}
        — same checks, same gate, same shared cooldown.
      </p>

      <div className="mb-6 grid gap-6 lg:grid-cols-2">
        <DriftPanel onRan={refreshHistory} />
        <QualityPanel onRan={refreshHistory} />
      </div>

      <div className="mb-6">
        {/* useSearchParams (the ?prediction_id= hand-off) needs a Suspense boundary at build time */}
        <Suspense fallback={<p className="text-caption-md text-ash">[~] loading labels…</p>}>
          <LabelsPanel />
        </Suspense>
      </div>

      <HistoryList ref={historyRef} />
    </>
  );
}
