# Data Model: 021 Loop-Native Operator Console

021 introduces **no new persisted data** — the console is a stateless read/write surface over
existing gateway endpoints. This document captures the **UI-facing view models**: the shapes the
console reads and the ephemeral client state it holds. Every field below already exists in a gateway
response; nothing here is a new backend entity.

## Read models (from existing endpoints)

### LoopStage (client-side, derived)
The nav's model of one lifecycle step.
- `key`: one of `data | training | models | serving | monitoring | retraining`
- `label`, `order`, `route`
- `badge`: live status glyph value (see StageBadgeSignal)

### StageBadgeSignal (derived from live state)
- `training`: active-run present? (boolean/indicator)
- `models`: candidate-awaiting-promotion? (boolean)
- `serving`: resident engine name | null (from LeaseState)
- `monitoring`: latest-breach? (boolean)
- `retraining`: open-suggestion count (integer)
- `state`: `live | at-rest | unknown` (unknown when platform unreachable)

### LeaseState — `GET /serving/state`
- `holder`: `llm | vision | training | null`
- `resident`: boolean
- `serving_model`, `serving_version`
- Powers the GPU pill (FR-211) and the serving LeaseView (FR-234). Read-only.

### ServingTask — `GET /serving/tasks`
- `task`: string task tag | null (null → NoRenderer placeholder)
- one entry per promoted `@serving` version → one panel (FR-231)

### DatasetVersion — `GET /datasets`, `GET /datasets/:name`, `GET /datasets/:name/:version`
- `name`, `version`, `size_bytes`, `sha256`, `format`, `uri`
- version detail adds the manifest + presigned download (FR-215)

### ValidationReport — `POST /datasets/:name/:version/validate`
- `passed`, `rules[]` (`name`, `passed`, `disposition: gate | warn`, `value`, `threshold`, `detail`)
- `stats` (`row_count`, `columns`), `gate_failures[]`, `warnings[]`
- rendered as the pre-train readiness gate (FR-216)

### RunRecord — `POST /runs` (202), `GET /runs/:id`, `GET /runs/:id/events` (SSE)
- `run_id`, `status`, `model` (`name`, `version` once registered), `metrics`, `error`
- 202/409(busy)/507(over-budget) launch outcomes surfaced as first-class (FR-222)

### StudyRecord — `POST /studies`, `GET /studies/:id`
- `study_id`, `status`, `best` (`version`, `value`, `metric`, `params`), `summary`

### ModelVersion — `GET /models`, `GET /models/:name`
- `name`, `version`, `source`, `run_id`, `tags{}`, `serving` (champion flag)
- lineage from `run_id` + tags (`dataset@version`, `base_model`, `parent`, `arch`, `task`,
  `serving_engine`) → drill-back links (FR-225); no `run_id` ⇒ seeded/imported (visually distinct)
- **Metric is NOT in this shape** — fetched on demand via evaluate (FR-226)

### GateVerdict — `POST /models/:name/promote`, `POST /models/:name/evaluate`, `.../compare`
- promote: `promoted` (did the alias move), `verdict` (`pass | warn | block`), `override` honored
- evaluate: logged metric (no reload); compare: per-metric champion↔challenger winner

### InferenceResult — `POST /infer/stream` (SSE) + siblings (`vision/classify`, `predict`, `embed`, `transcribe`)
- LLM: completion text, `registry_version`, `prediction_id`, `load_ms` (FR-233)
- each result logs a prediction + captures input → the serving→monitoring seam

### BatchJob — `POST /batch` (202), `GET /batch/:id`
- launch over `dataset@version` → poll `status` → result link (FR-236)

### DriftReport — `POST /monitor/check`, `GET /monitor`
- `report` (`dataset_drift`, PSI stats), `retrain` (`launched | {skipped: cooldown} | {error}`)

### QualityReport — `POST /monitor/quality/check`, `GET /monitor/quality`
- `report` (`breach`, windowed metric vs baseline), `retrain` (same OR+cooldown shape)
- inputs: `model_version`, `modality`, `window_n>0`, `drop_pct`, optional `baseline`

### Label — `POST /monitor/labels`
- `prediction_id` + `label` → status (recorded | late | duplicate | unknown-id), never overwrites
  served history (FR-239)

### Policy — `GET /policies`, `GET /policies/:model`, `PUT`/`DELETE`
- whole validated document; invalid ⇒ structured 400 `{errors:[...]}` shown inline (FR-243)
- includes the **auto-promote** flag (off by default; warned opt-in — FR-245)

### PolicyStatus — `GET /policies/:model/status`
- `last_check`, `next_due`, `pending_retrain` → the per-model cycle board (FR-246)

### Suggestion — `GET /suggestions?state=`, `POST .../accept`, `POST .../dismiss`
- `suggestion_id`, `model_name`, `candidate_version`, `state: open | accepted | dismissed`, breach
  signal, gate verdict on accept
- accept routes through the gated promote; a block keeps it `open` → override deep-link (FR-247)

### HealthState — `GET /platform/health`, `GET /platform/events` (SSE), per-engine `*/health`
- platform liveness + per-engine probe dot (serving/predict/vision/embed/transcribe/training) (FR-249)

## Ephemeral client state (not persisted)

- **Hand-off params** (URL query, R7): `dataset@version`, registered version, `prediction_id`,
  blocked candidate — passed stage→stage, read once on load.
- **Confirm-dialog state**: pending high-trust action + captured reason (override) — transient.
- **Live subscriptions**: SSE connections for badges, lease pill, run logs — opened per view, closed
  on unmount.

## State transitions worth noting (all backend-owned; the UI only reflects them)

- **Suggestion**: `open → accepted` (gated promote succeeds) | `open → dismissed`; a blocked accept
  stays `open`.
- **Promotion alias**: unmoved on `block` (unless override-with-reason); moved on `pass`/`warn`.
- **Retrain reservation**: `launched | skipped:cooldown | error` — shared OR+cooldown across both
  breach signals; the UI renders the returned outcome, never computes it.
