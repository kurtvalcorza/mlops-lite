'use client';

// 027 — Evaluations area index. Gates and comparisons are Phases 8's work; drift is live today, so
// this page links to what exists rather than rendering placeholders for what does not. An area that
// shows empty panels for unbuilt views teaches an operator that the console is unreliable.

import Link from 'next/link';
import { Panel, PageTitle } from '@/components/Panel';

export default function EvaluationsPage() {
  return (
    <div>
      <PageTitle sub="Quality gates, comparisons, and drift">Evaluations</PageTitle>
      <Panel title="Available now">
        <ul className="text-body-md">
          <li>
            <Link href="/evaluations/drift" className="underline">
              Drift
            </Link>{' '}
            — drift reports, policies, and the retrain cycle
          </li>
        </ul>
        <p className="mt-3 text-caption-md text-ash">
          Gate detail and champion/challenger comparison are not built yet. They are listed in
          specs/027-unified-lifecycle-console/tasks.md rather than shown here as empty panels.
        </p>
      </Panel>
    </div>
  );
}
