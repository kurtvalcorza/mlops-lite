'use client';

// 027 US9 (T759) — Administration: storage, the migration ledger, integrations, and system identity.
//
// **No credential material appears anywhere on this page**, and not because it is filtered out
// here — because the routes behind it never read one. `apiAccess` reports whether a key is
// configured and whether the gateway is fail-closed; `integrations[].endpoint` is a host identity
// with any `user:password@` stripped before it left the process. An admin page is exactly where
// someone pastes a screenshot into a ticket.
//
// The migration ledger is read-only and there is deliberately no apply control. A console that
// could migrate a database from a page render is a console one misclick from an outage.

import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { CADENCE, formatAge, useLive } from '@/lib/use-live';
import type {
  AdminDatabase,
  AdminIntegration,
  AdminStorage,
  AdminSystem,
  Capabilities,
} from '@/lib/platform-types';

export default function AdministrationPage() {
  const capabilities = useLive<Capabilities>('console/capabilities', CADENCE.admin);
  const storage = useLive<AdminStorage[]>('console/admin/storage', CADENCE.admin);
  const database = useLive<AdminDatabase>('console/admin/database', CADENCE.admin);
  const integrations = useLive<AdminIntegration[]>('console/admin/integrations', CADENCE.admin);
  const system = useLive<AdminSystem>('console/admin/system', CADENCE.admin);

  return (
    <div>
      <PageTitle sub="Storage, schema, integrations, and platform identity">
        Administration
      </PageTitle>

      <div className="flex flex-col gap-6">
        <Panel title="System">
          <p className="mb-2 text-caption-md text-ash">{formatAge(system.ageMs)}</p>
          {!system.data ? (
            <p className="text-body-md text-mute">unknown</p>
          ) : (
            <dl className="font-mono text-body-md">
              <Row label="platform" value={system.data.platformVersion} />
              <Row label="constitution" value={system.data.constitutionVersion} />
              <Row label="host" value={system.data.host} />
              <Row
                label="uptime"
                value={
                  system.data.uptimeSeconds === null
                    ? null
                    : `${Math.floor(system.data.uptimeSeconds / 60)}m`
                }
              />
              {/* Whether a key is configured — never the key. */}
              <Row
                label="api access"
                value={`${system.data.apiAccess.keyConfigured ? 'key configured' : 'NO KEY'} · ${
                  system.data.apiAccess.failClosed ? 'fail-closed' : 'FAIL-OPEN'
                }`}
              />
            </dl>
          )}
        </Panel>

        <Panel title="Storage">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(storage.ageMs)}
            {storage.degraded.length > 0 && ` · unreachable: ${storage.degraded.join(', ')}`}
          </p>
          {!storage.data ? (
            <p className="text-body-md text-mute">unknown — the object store did not answer</p>
          ) : (
            <ul className="font-mono text-body-md text-mute">
              {storage.data.map((bucket) => (
                <li key={bucket.bucket}>
                  {bucket.bucket}:{' '}
                  {/* `null` objects on an unreachable bucket, NOT 0 — "0 objects" reads as empty. */}
                  {bucket.objectCount === null ? (
                    <span className="text-ash">unknown</span>
                  ) : (
                    <span className="text-ink">{bucket.objectCount} object(s)</span>
                  )}
                  {!bucket.reachable && <span className="text-ash"> · unreachable</span>}
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Database">
          <p className="mb-2 text-caption-md text-ash">
            {formatAge(database.ageMs)}
            {database.degraded.length > 0 && ` · unreachable: ${database.degraded.join(', ')}`}
          </p>
          {!database.data?.migrations ? (
            <p className="text-body-md text-mute">unknown — the ledger could not be read</p>
          ) : (
            <>
              <p className="font-mono text-body-md text-ink">
                schema {database.data.schemaVersion}
              </p>
              <ul className="mt-2 font-mono text-body-md text-mute">
                {database.data.migrations.map((migration) => (
                  <li
                    key={migration.id}
                    className={migration.checksumState === 'mismatch' ? 'text-ink' : ''}
                  >
                    {migration.id} · {migration.checksumState}
                    {migration.appliedAt && (
                      <span className="text-ash"> · {migration.appliedAt}</span>
                    )}
                  </li>
                ))}
              </ul>
              {/* Stated, so nobody looks for the button. */}
              <p className="mt-2 text-caption-md text-ash">
                [i] read-only. Migrations are applied by the platform&apos;s own tooling, never from
                here.
              </p>
            </>
          )}
        </Panel>

        <Panel title="Integrations">
          {!integrations.data ? (
            <p className="text-body-md text-mute">unknown</p>
          ) : (
            <ul className="font-mono text-body-md text-mute">
              {integrations.data.map((integration) => (
                <li key={integration.name}>
                  {integration.name}: {integration.endpoint ?? 'not configured'}
                  {integration.reachable === null ? (
                    <span className="text-ash"> · reachability not probed</span>
                  ) : (
                    <span className="text-ash">
                      {' '}
                      · {integration.reachable ? 'reachable' : 'unreachable'}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Tenants and quotas">
          {capabilities.data && !capabilities.data.broker ? (
            // Omitted rather than offered-and-broken.
            <p className="text-body-md text-mute">
              The broker is not enabled on this deployment, so there are no tenants to administer.
            </p>
          ) : (
            <ul className="text-body-md">
              <li>
                <Link href="/broker" className="underline">
                  Broker
                </Link>{' '}
                <span className="text-mute">
                  — GPU residency, both VRAM bounds, tenants, quotas, and the usage ledger
                </span>
              </li>
            </ul>
          )}
        </Panel>
      </div>
    </div>
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
