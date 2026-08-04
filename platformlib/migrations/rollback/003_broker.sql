-- Rollback for 003_broker (026 T618).
--
-- Lives OUTSIDE `platformlib/migrations/` on purpose: `migrations.discover()` fails loud on any
-- `.sql` file in that directory that does not match `NNN_name.sql`, and a rollback is not a
-- migration — applying it forward would be a schema regression. `scripts/migrate_db.py rollback`
-- and `tests/test_broker_migration.py` are the only callers.
--
-- Drops in reverse dependency order, then retracts the ledger row so a re-apply is clean.

DROP TABLE IF EXISTS broker_sessions;
DROP TABLE IF EXISTS broker_jobs;
DROP TABLE IF EXISTS usage_reservation;
DROP TABLE IF EXISTS usage_ledger;
DROP TABLE IF EXISTS quotas;
DROP TABLE IF EXISTS api_keys;
DROP TABLE IF EXISTS tenants;

DELETE FROM schema_migrations WHERE version = 3;
