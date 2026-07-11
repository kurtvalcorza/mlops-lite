# Contract: Relational Schema Migrations

## Source of truth

Ordered SQL files under `platformlib/migrations/` are the only application-schema authority. Both
fresh databases and upgrades use the same runner. `infra/postgres/init.sql` creates the `gateway`
database/ownership needed before the application can connect but does not duplicate tables,
indexes, or application columns.

## File format

```text
001_baseline.sql
002_<short_description>.sql
003_<short_description>.sql
```

- Numeric prefix is unique, positive, and strictly increasing.
- File bytes are immutable after merge/application.
- SHA-256 is computed over exact normalized repository bytes according to one documented rule.
- Descriptions contain no runtime branching; environment-specific behavior belongs in the runner.

## Ledger and apply algorithm

1. Connect with autocommit disabled.
2. Acquire the fixed Postgres advisory lock for MLOps-Lite schema evolution.
3. Create/read `schema_migrations` through the bootstrap-safe minimal statement.
4. Verify every recorded version exists locally and its checksum matches.
5. Refuse a recorded version greater than the binary's current supported version.
6. Apply each pending file in order, in its own transaction.
7. Insert ledger row in the same transaction as its schema change.
8. Commit, report duration, proceed; on failure roll back and stop.
9. Release lock/connection.

Concurrent runners wait on the advisory lock and then observe the committed ledger; a version is
applied once.

## Existing database adoption

For an installation created by the old duplicated DDL:

- Inspect required baseline tables, columns, indexes, primary/foreign keys, and constraints.
- If the shape exactly matches the recognized baseline, insert the `001` ledger row without
  reapplying destructive statements.
- If it is missing additive objects that `001` can safely create, apply the baseline idempotently
  and verify the final shape before stamping.
- If shape conflicts, fail with a report and require operator remediation; never guess.

## Compatibility checks

- Gateway startup owns apply by default and fails readiness on migration error.
- Agent/tools that need the store check `minimum <= database_version <= supported_current` before
  writes.
- A newer schema fails writes closed but permits minimal health output explaining incompatibility.
- A checksum mismatch is never auto-repaired.

## Evolution policy

- Additive migrations are preferred.
- Destructive/renaming changes use expand/contract across releases.
- Down migrations are not required; restore/forward-fix is the recovery posture.
- Before first upgrade of populated state, operator creates and restores a `pg_dump` in a disposable
  database as documented in quickstart.

## Observability

Expose last applied version, pending count, outcome counter, and apply duration without database URL
or SQL text. A failure alert links the migration section of the quickstart.
