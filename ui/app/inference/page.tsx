'use client';

// 027 — Inference area. US6's full scope (the prediction record, trace detail, capture/label
// browsing) is Phase 9's work and its backend reads are not built. What exists today is the
// request-and-response panel set 021 shipped, so this area presents that and says plainly what is
// not here yet — rather than rendering empty panels for views with no data behind them.

import Link from 'next/link';
import { Panel, PageTitle } from '@/components/Panel';

export default function InferencePage() {
  return (
    <div>
      <PageTitle sub="Send a request; read the record it produced">Inference</PageTitle>
      <Panel title="Available now">
        <ul className="text-body-md">
          <li>
            <Link href="/deployments" className="underline">
              Deployments
            </Link>{' '}
            — the per-modality request panels, alongside what is currently serving
          </li>
          <li>
            <Link href="/runtime" className="underline">
              Runtime
            </Link>{' '}
            — which model actually answered, and the admission decision behind it
          </li>
        </ul>
        <p className="mt-3 text-caption-md text-ash">
          The prediction record, trace waterfall, and capture/label browsing need gateway read
          surfaces that are not built yet (specs/027-unified-lifecycle-console/tasks.md, Phase 9).
          They are listed there rather than stubbed here.
        </p>
      </Panel>
    </div>
  );
}
