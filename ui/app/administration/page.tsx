'use client';

// 027 — Administration area. The broker's tenants, quotas, and usage ledger are the administration
// surface that exists today (026); the rest of US9's scope is Phase 12.

import Link from 'next/link';
import { Panel, PageTitle } from '@/components/Panel';
import { CADENCE, useLive } from '@/lib/use-live';
import type { Capabilities } from '@/lib/platform-types';

export default function AdministrationPage() {
  const { data } = useLive<Capabilities>('console/capabilities', CADENCE.admin);

  return (
    <div>
      <PageTitle sub="Tenants, quotas, and the usage ledger">Administration</PageTitle>
      <Panel title="Available now">
        {data && !data.broker ? (
          // Omitted rather than offered-and-broken: the broker is disabled on this deployment.
          <p className="text-body-md text-mute">
            The broker is not enabled on this deployment, so there are no tenants to administer.
          </p>
        ) : (
          <ul className="text-body-md">
            <li>
              <Link href="/broker" className="underline">
                Broker
              </Link>{' '}
              — GPU residency, both VRAM bounds, the queue, tenants, quotas, and the ledger
            </li>
          </ul>
        )}
        <p className="mt-3 text-caption-md text-ash">
          Configuration inspection and alert-rule administration are Phase 12 and are not built yet.
        </p>
      </Panel>
    </div>
  );
}
