# Contract: Relational store schema + backfill (US4)

Database: the provisioned `gateway` DB (Postgres 17, already resident). Applied by
`platformlib.store.bootstrap()` idempotently at client init and mirrored in
`infra/postgres/init.sql` (R4). Additive-only within 018; `meta(schema_version)` single row.

```sql
CREATE TABLE IF NOT EXISTS meta (schema_version int NOT NULL);

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
-- PRIMARY KEY = the write-once constraint (FR-185): second insert -> unique violation
-- surfaced as LabelExists; no in-process lock involved.

CREATE TABLE IF NOT EXISTS capture_index (
  prediction_id text PRIMARY KEY,
  modality      text NOT NULL,
  input_ref     text NOT NULL,      -- MinIO key; payload stays in object storage
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
  document   jsonb NOT NULL,        -- validated ModelPolicy
  updated_at timestamptz NOT NULL,
  updated_by text NOT NULL
);
-- T375: the queue-of-one parked retrain + the last per-model check status are strictly 1:1 with the
-- policy (keyed by model_name), so they fold onto the row instead of the pre-US4 policies/_pending/ +
-- policies/_status/ objects. Additive (ADD COLUMN IF NOT EXISTS) → no schema-version bump.
ALTER TABLE policies ADD COLUMN IF NOT EXISTS pending jsonb;
ALTER TABLE policies ADD COLUMN IF NOT EXISTS status  jsonb;

CREATE TABLE IF NOT EXISTS suggestions (
  id                text PRIMARY KEY,
  model_name        text NOT NULL,
  candidate_version text NOT NULL,
  gate_verdict      jsonb NOT NULL,
  shadow_verdict    jsonb,
  state             text NOT NULL,  -- open|accepted|dismissed|auto-promoted
  created_at        timestamptz NOT NULL,
  resolved_at       timestamptz,
  actor             text
);
```

## Window query (replaces O(N) object reads)

`window(modality, model, version, n)` = one indexed join predictions⋈labels ordered
`served_at DESC LIMIT n` — the quality (013) and shadow (016) resolution path (SC-111).
TTL filtering moves into the WHERE clause.

## Backfill (one-time, idempotent)

`scripts/backfill_store.py`: stream `results/predictions/*`, `labels/*`, `inputs/*` objects →
`INSERT … ON CONFLICT DO NOTHING` keyed by `prediction_id`; report counts; leave objects in
place (reports stay readable; payload refs already point at MinIO). Agent journal JSONL is
imported into `jobs` the same way, then the JSONL path retires (R9).

## Failure posture

Store down ⇒ prediction/label/capture writes degrade fail-open with dropped-counters (spec
edge case; FR-164 metric); window/policy/job reads fail loud 502. Serving never blocks on the
store.
