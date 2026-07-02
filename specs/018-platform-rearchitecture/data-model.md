# Phase 1 Data Model: 018 Platform Re-Architecture

Entities span three homes: **agent memory** (rebuilt from journal), **journal/relational store**
(durable), **MinIO** (blobs). Typed shapes live in `platformlib.contracts`; this file is the
source of truth for fields and transitions.

## Tenant (agent memory)

The current GPU occupant. At most one exists (Principle II).

| Field | Type | Notes |
|---|---|---|
| `tenant_id` | enum `platformlib.topology.Tenant` | `llm`, `asr`, `vision`, `training` (CPU engines never appear) |
| `kind` | enum | `serving` \| `job` — decides preemptability (serving only) |
| `est_vram_gb` | float | admission estimate (per-engine heuristic, unchanged from today) |
| `child_pid` | int | the VRAM-owning child process |
| `acquired_at` / `last_used` | monotonic ts | idle-reaper input |
| `state` | enum | `loading` → `ready` → (`draining` \| `idle-releasing`) → `unloaded`; `wedged` terminal-until-operator |

Transitions enforced by `hostagent.lifecycle`; `wedged` (kill failed, child in D-state) keeps
the slot occupied, surfaces on health/metrics, and refuses admissions (spec edge case).

## EngineAdapter (static registry, `platformlib.topology`)

| Field | Type | Notes |
|---|---|---|
| `engine_id` | str | `llm`, `asr`, `vision`, `embed`, `tabular` |
| `gpu` | bool | `embed`/`tabular` false → exempt from admission |
| `optional` | bool | `asr` true — `unavailable` never blocks `all_healthy` (R7) |
| `state` | enum | `disabled` \| `unavailable(reason)` \| `cold` \| tenant state when resident |
| adapter surface | code | `spawn() -> child`, `ready() -> bool`, `forward(request) -> response`, `drain()`, `estimate_vram()` |

## JobRecord (journal → relational store at US4)

| Field | Type | Notes |
|---|---|---|
| `job_id` | str (uuid) | returned to callers; gateway `GET /runs/{id}` etc. resolve here |
| `kind` | enum | `train` \| `hpo` \| `batch` \| `shadow` |
| `modality` | str | validated against `flow_dispatch.VALID_MODALITIES` |
| `request` | json | the submitted spec (dataset, params, parent_version…) |
| `state` | enum | `queued` → `running` → `succeeded` \| `failed(reason)` \| `interrupted(reason)` |
| `submitted_at` / `started_at` / `ended_at` | ts | |
| `result` | json \| null | run_flow result payload (registered version, metrics) |

State machine: journal writes on every transition (FR-173). Agent restart: any `running` row →
`interrupted("agent restart")` + alert metric increment. `interrupted` is the only
restart-authored state.

## ModelPolicy (MinIO object pre-US4 → `policies` table at US4)

| Field | Type | Notes |
|---|---|---|
| `model_name` | str (pk) | registry model this policy governs |
| `modality` | str | drives on-breach retrain flow (FR-181) |
| `monitors` | list | subset of `input_drift`, `quality`; each with its params (window, min score / PSI threshold, reference) |
| `check_interval_s` | int ≥ 60 | scheduler tick |
| `on_breach` | obj | `{action: retrain, dataset: "latest" \| pinned, params: {...}}` |
| `promotion_mode` | enum | `manual` (default) \| `suggest` \| `auto-on-green` |
| `enabled` | bool | pause without delete |
| `updated_at` / `updated_by` | ts / str | audit trail of edits |

Validation at write time (FR-179): known model + modality with a fine-tune flow, ≥1 monitor,
interval bound, params complete. Invalid → structured 400, never stored.

## PendingRetrain (scheduler state, durable beside policies)

Queue-of-one per model (FR-182): `model_name`, `breach` (signal, score, at), `attempts`,
`next_attempt_at`. Superseded by a newer breach for the same model; cleared on launch.

## PromotionSuggestion / AuditRecord (store at US4; MinIO objects before)

| Field | Type | Notes |
|---|---|---|
| `id` | str | |
| `model_name` / `candidate_version` | str | |
| `gate_verdict` | json | from `evaluation.gate` |
| `shadow_verdict` | json \| null | latest window verdict when one exists (FR-183) |
| `state` | enum | suggestion: `open` → `accepted` \| `dismissed`; audit: `auto-promoted` |
| `created_at` / `resolved_at` / `actor` | | `actor` = operator or `policy:<model>` for auto |

## Prediction / Label / CaptureIndex rows (US4, `store-schema.md` has DDL)

- **predictions**: `prediction_id` pk, `model_name`, `version`, `modality`, `served_at`,
  `streamed` bool, `payload_ref` (MinIO key or null). Indexed `(modality, model_name, version,
  served_at desc)` — the window query.
- **labels**: `prediction_id` pk **+ unique constraint = write-once** (FR-185), `label`,
  `submitted_at`.
- **capture_index**: `prediction_id` pk, `modality`, `input_ref` (MinIO key), `captured_at`;
  payloads stay in MinIO (FR-184).

Backfill maps existing `results/predictions|labels|inputs/*` objects 1:1 into these tables
(one-time, idempotent on `prediction_id`).

## Journal entry (JSONL, pre-US4 — R9)

`{ts, job_id, transition, from, to, reason?, result_ref?}` — replay folds left to rebuild
JobRecords; unknown fields ignored (forward-compatible).
