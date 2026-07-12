-- 001_baseline: the pre-023 gateway-database schema, verbatim (023 US4 T512, FR-297/300).
--
-- IMMUTABLE after merge — the ledger checksums these exact bytes; evolve with a NEW numbered file.
-- Additive-only (every statement IF NOT EXISTS / ADD COLUMN IF NOT EXISTS) so the same file both
-- creates a fresh database and idempotently ADOPTS an installation created by the retired
-- duplicated bootstrap (platformlib/store.py:DDL + infra/postgres/init.sql, both retired at T513);
-- the runner verifies the final shape before stamping this version.
-- Provenance: contracts/store-schema.md (018 T373) + 018 T375 policy columns + 022 T461 serving_llm.

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
-- 018 T375: queue-of-one parked retrain + last check status ride the policy row.
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

-- 022 T461: the single active serving-LLM pointer (one row; absent => the default base serves).
CREATE TABLE IF NOT EXISTS serving_llm (
  singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  model_name  text NOT NULL,
  selected_at timestamptz NOT NULL,
  selected_by text NOT NULL
);
