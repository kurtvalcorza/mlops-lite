# Data Model: MLOps-Lite Platform

Entities derived from `spec.md` (Key Entities). Each entity notes its **store of record**:
**PG** = PostgreSQL (gateway metadata + MLflow backend), **MinIO** = object storage,
**MLflow** = MLflow tracking/registry (itself on PG + MinIO).

## Entities

### Dataset / DatasetVersion — store: PG (metadata) + MinIO (content) via DVC
- `Dataset`: `id`, `name` (unique), `created_at`.
- `DatasetVersion`: `id`, `dataset_id` → Dataset, `version` (int, auto-increment per dataset),
  `content_uri` (MinIO/DVC pointer, immutable), `item_count`, `validated` (bool), `created_at`.
- Rule: a new registration of changed content creates a new `DatasetVersion`; prior versions
  are never mutated (FR-007).

### Model / ModelVersion — store: MLflow registry (PG + MinIO artifacts)
- `Model`: `name` (unique registry name), `modality` (`text`|`vision`|`audio`), `created_at`.
- `ModelVersion`: `name` → Model, `version` (int, auto), `source_run_id` → Run (nullable for
  foundational models), `artifact_uri` (MinIO), `size_bytes`, `stage` (`none`|`serving`),
  `metrics` (JSON), `created_at`.
- Rule: exactly one `ModelVersion` per Model may hold `stage = serving` at a time (FR-005/FR-006).
- Rule: `size_bytes` must fit `VRAM_GB`; oversize versions are rejected at register/serve (FR-004).

### Run — store: MLflow (PG + MinIO)  *(canonical term; formerly "ExperimentRun")*
- `Run`: `id`, `type` (`finetune`), `status` (`pending`|`running`|`succeeded`|`failed`|`cancelled`),
  `base_model_version` → ModelVersion, `dataset_version_id` → DatasetVersion, `params` (JSON),
  `metrics` (JSON, time-series), `logs_uri` (MinIO), `output_model_version` → ModelVersion
  (set on success), `started_at`, `ended_at`.
- Rule: on `succeeded`, exactly one `output_model_version` is registered and linked (FR-009).
- Rule: `failed`/`cancelled` leaves no partial `ModelVersion` and frees the GPU (edge cases).

### InferenceRequest / InferenceResult — store: PG (records) + MinIO (payloads/outputs)
- `InferenceRequest`: `id` (task id), `model_version` → ModelVersion, `modality`,
  `input_uri_or_text`, `status` (`queued`|`loading`|`running`|`completed`|`failed`),
  `created_at`.
- `InferenceResult`: `request_id` → InferenceRequest, `output_uri_or_text`, `load_ms` (cold-start),
  `infer_ms`, `error` (nullable), `completed_at`.
- Rule: `load_ms` is reported separately from `infer_ms` (FR-002, T017).

### DriftReport — store: PG (summary) + MinIO (full report)
- `id`, `model_name`, `reference_profile_uri`, `current_window`, `signals` (JSON: drift/quality),
  `threshold_breached` (bool), `triggered_run_id` → Run (nullable), `created_at`.
- Rule: `threshold_breached = true` triggers a `Run` (FR-011) and records `triggered_run_id`.

### ServingState — store: PG (authoritative) + runtime (VRAM residency)
- `serving_model_version` per Model (mirror of `ModelVersion.stage = serving`).
- `resident_model_version` (nullable): the single model currently in VRAM.
- Invariant (NON-NEGOTIABLE, Principle II): at most one `resident_model_version` across the
  whole platform at any instant.

## Relationships (summary)

```
Dataset 1─* DatasetVersion ─┐
                            ├─* Run *─1 ModelVersion(base)
Model 1─* ModelVersion ─────┘        Run 1─0..1 ModelVersion(output)
ModelVersion 1─* InferenceRequest 1─1 InferenceResult
Model 1─* DriftReport 0..1─* Run(triggered)
ServingState 1─1 ModelVersion(resident, ≤1 global)
```

## Lifecycle invariants
- One model in VRAM at a time (`ServingState.resident_model_version`) — Principle II.
- Every `Run`, `ModelVersion`, and `DatasetVersion` is immutable once recorded and fully
  retrievable (FR-012 / SC-005) — reproducibility.
- All artifacts (datasets, weights, results, logs, reports) live in MinIO buckets:
  `datasets`, `models`, `results`, `mlflow`.
