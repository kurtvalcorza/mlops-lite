-- POSTGRES_DB creates the `mlflow` database. Create the gateway DB alongside it.
CREATE DATABASE gateway;

-- US4 (T373): mirror platformlib.store.DDL into the gateway DB on a fresh volume so the monitoring
-- schema is present before first use. platformlib.store.bootstrap() ALSO applies this idempotently at
-- client init (every statement is IF NOT EXISTS), so the two can never drift — this is belt-and-braces
-- for a clean box. Keep in lockstep with contracts/store-schema.md and platformlib/store.py:DDL.
\connect gateway

CREATE TABLE IF NOT EXISTS meta (schema_version int NOT NULL);
INSERT INTO meta (schema_version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM meta);

CREATE TABLE IF NOT EXISTS predictions (
  prediction_id text PRIMARY KEY,
  model_name    text NOT NULL,
  version       text NOT NULL,
  modality      text NOT NULL,
  served_at     timestamptz NOT NULL,
  streamed      boolean NOT NULL DEFAULT false,
  payload_ref   text
);
CREATE INDEX IF NOT EXISTS ix_pred_window
  ON predictions (modality, model_name, version, served_at DESC);

CREATE TABLE IF NOT EXISTS labels (
  prediction_id text PRIMARY KEY REFERENCES predictions(prediction_id),
  label         jsonb NOT NULL,
  submitted_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_index (
  prediction_id text PRIMARY KEY,
  modality      text NOT NULL,
  input_ref     text NOT NULL,
  captured_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id       text PRIMARY KEY,
  kind         text NOT NULL,
  modality     text NOT NULL,
  request      jsonb NOT NULL,
  state        text NOT NULL,
  submitted_at timestamptz NOT NULL,
  started_at   timestamptz,
  ended_at     timestamptz,
  result       jsonb
);
CREATE INDEX IF NOT EXISTS ix_jobs_kind ON jobs (kind, submitted_at DESC);

CREATE TABLE IF NOT EXISTS policies (
  model_name text PRIMARY KEY,
  document   jsonb NOT NULL,
  updated_at timestamptz NOT NULL,
  updated_by text NOT NULL
);
-- US4 T375: the queue-of-one parked retrain + last check status fold onto the policy row.
ALTER TABLE policies ADD COLUMN IF NOT EXISTS pending jsonb;
ALTER TABLE policies ADD COLUMN IF NOT EXISTS status  jsonb;

CREATE TABLE IF NOT EXISTS suggestions (
  id                text PRIMARY KEY,
  model_name        text NOT NULL,
  candidate_version text NOT NULL,
  gate_verdict      jsonb NOT NULL,
  shadow_verdict    jsonb,
  state             text NOT NULL,
  created_at        timestamptz NOT NULL,
  resolved_at       timestamptz,
  actor             text
);
