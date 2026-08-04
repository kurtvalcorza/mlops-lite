-- 003_broker: LAN self-service GPU broker tenancy, quota, metering, jobs, sessions
-- (026 T618 — data-model.md §Persistent entities).
--
-- IMMUTABLE after merge — evolve with a new numbered file. The paired rollback lives in
-- `platformlib/migrations/rollback/003_broker.sql` (outside this directory so the forward-only
-- discoverer never picks it up as a migration of its own).
--
-- Five broker tables plus the broker-owned columns on `jobs`. The design points worth stating in
-- the schema itself, because they are the ones a later reader would otherwise "simplify" away:
--
--   * `usage_reservation.window_start` is stamped at RESERVE time and every charge — reserve,
--     settle, release — is made against THAT window, never the window in force at completion. A
--     job reserved just before a boundary and finishing after it would otherwise be authorized
--     against the old window while its ledger row landed in the new one, letting the tenant spend
--     the new window's full budget before the old job settled (data-model.md §Window binding).
--   * `usage_ledger` is append-only. There is no UPDATE path; a correction is a new row.
--   * `job.state` carries `interrupted` as a DISTINCT terminal state — a broker-caused restart is
--     not a tenant-code failure, and in a metered broker that difference is the tenant's basis for
--     disputing a charge (data-model.md §Restart recovery).

-- -- tenants --------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
  id          text PRIMARY KEY,
  name        text NOT NULL UNIQUE CHECK (name <> ''),
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  is_system   boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- At most ONE reserved system tenant (T621): policy-triggered retrains are tenant-less work that
-- still must be metered and lane-ordered, so they run as this tenant rather than bypassing both.
CREATE UNIQUE INDEX IF NOT EXISTS tenants_one_system
  ON tenants ((true)) WHERE is_system;

-- -- api keys -------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_keys (
  id          text PRIMARY KEY,
  tenant_id   text NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  key_hash    text NOT NULL UNIQUE,
  prefix      text NOT NULL CHECK (prefix <> ''),
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz,
  -- a revoked key carries its revocation time; an active one never does
  CONSTRAINT api_keys_revoked_at_matches_status
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant_id);

-- The auth hot path looks a key up by its hash and must see the tenant's status in the same read.
CREATE INDEX IF NOT EXISTS api_keys_hash_active ON api_keys (key_hash) WHERE status = 'active';

-- -- quotas ---------------------------------------------------------------------------------------

-- `quota_window`, not `window`: `window` is a reserved word in Postgres (SQL:2003 window
-- functions), so an unquoted reference is a syntax error and a quoted one would have to stay quoted
-- at every call site forever. data-model.md calls the field `window`; this is the same field.
CREATE TABLE IF NOT EXISTS quotas (
  tenant_id           text PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
  quota_window        text NOT NULL CHECK (quota_window IN ('daily', 'weekly', 'monthly')),
  budget_gpu_seconds  bigint NOT NULL CHECK (budget_gpu_seconds >= 0),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- -- usage ledger (append-only) ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS usage_ledger (
  id            bigserial PRIMARY KEY,
  tenant_id     text NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('inference', 'job', 'session')),
  ref_id        text NOT NULL CHECK (ref_id <> ''),
  modality      text NOT NULL DEFAULT '',
  gpu_seconds   numeric NOT NULL CHECK (gpu_seconds >= 0),
  window_start  timestamptz NOT NULL,
  ts            timestamptz NOT NULL DEFAULT now()
);

-- One settled row per operation: the settle step is idempotent under retry (T629's outbox replay
-- and the restart-recovery sweep both re-run it), so the uniqueness has to be enforced here rather
-- than assumed from the caller.
CREATE UNIQUE INDEX IF NOT EXISTS usage_ledger_ref ON usage_ledger (ref_id);

-- Window consumption is derived by summing rows BEARING a window_start, not by filtering on `ts`.
CREATE INDEX IF NOT EXISTS usage_ledger_tenant_window ON usage_ledger (tenant_id, window_start);

-- -- usage reservations (idempotent pre-authorization) ----------------------------------------------

CREATE TABLE IF NOT EXISTS usage_reservation (
  op_id             text PRIMARY KEY,
  tenant_id         text NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  kind              text NOT NULL CHECK (kind IN ('inference', 'job', 'session')),
  modality          text NOT NULL DEFAULT '',
  window_start      timestamptz NOT NULL,
  est_gpu_seconds   numeric NOT NULL CHECK (est_gpu_seconds >= 0),
  settled_gpu_seconds numeric,
  state             text NOT NULL DEFAULT 'reserved'
                    CHECK (state IN ('reserved', 'settled', 'released')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  settled_at        timestamptz,
  -- a reservation leaves `reserved` exactly once, and only then carries its settlement stamps
  CONSTRAINT usage_reservation_settled_shape
    CHECK ((state = 'reserved') = (settled_at IS NULL))
);

-- The quota check sums OUTSTANDING reservations for a tenant's window; this is its index.
CREATE INDEX IF NOT EXISTS usage_reservation_open
  ON usage_reservation (tenant_id, window_start) WHERE state = 'reserved';

-- -- broker jobs ------------------------------------------------------------------------------------
--
-- The 001 baseline `jobs` table is the host agent's own journal and keeps its shape. Broker jobs are
-- a separate lane with tenancy, queue position, and a sandbox — modelling them as extra nullable
-- columns on the agent's table would make every legacy read carry broker semantics it has no use
-- for, and the two have genuinely different state machines (`interrupted` is terminal here).

CREATE TABLE IF NOT EXISTS broker_jobs (
  id            text PRIMARY KEY,
  tenant_id     text NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('batch', 'finetune', 'hpo')),
  spec          jsonb NOT NULL DEFAULT '{}'::jsonb,
  state         text NOT NULL DEFAULT 'queued' CHECK (state IN
                  ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
  queue_pos     integer,
  sandbox       text NOT NULL DEFAULT '',
  artifact_ref  text,
  model_version text,
  gpu_seconds   numeric,
  created_at    timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  ended_at      timestamptz,
  -- queue_pos exists exactly while queued: a running or terminal job holds no lane position, and a
  -- queued one without a position could not be ordered (T647's restart recovery reads this order).
  CONSTRAINT broker_jobs_queue_pos_when_queued
    CHECK ((state = 'queued') = (queue_pos IS NOT NULL))
);

-- FIFO order within the lane. Partial so terminal jobs never collide on a recycled position.
CREATE UNIQUE INDEX IF NOT EXISTS broker_jobs_queue_order
  ON broker_jobs (queue_pos) WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS broker_jobs_tenant ON broker_jobs (tenant_id, created_at DESC);

-- At most ONE running job platform-wide — the exclusive claim, enforced by the store rather than
-- only by the coordinator's in-memory `exclusive_job` (which does not survive a restart).
CREATE UNIQUE INDEX IF NOT EXISTS broker_jobs_one_running
  ON broker_jobs ((true)) WHERE state = 'running';

-- -- sessions ---------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS broker_sessions (
  id                    text PRIMARY KEY,
  tenant_id             text NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  state                 text NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active', 'idle', 'released', 'expired')),
  idle_timeout_s        integer NOT NULL CHECK (idle_timeout_s > 0),
  ttl_s                 integer NOT NULL CHECK (ttl_s > 0),
  -- idle-cull keys on GPU activity ALONE; a notebook's liveness heartbeat must never reset it, or
  -- an abandoned session holds the GPU for its whole TTL (data-model.md §Why two timestamps).
  last_gpu_activity_at  timestamptz,
  last_heartbeat_at     timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  ended_at              timestamptz
);

CREATE INDEX IF NOT EXISTS broker_sessions_tenant ON broker_sessions (tenant_id, created_at DESC);
