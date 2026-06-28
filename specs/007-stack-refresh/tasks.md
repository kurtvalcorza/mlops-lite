---
description: "Task list for Stack Refresh & MLflow 3.x Upgrade (007)"
---

# Tasks: Stack Refresh & MLflow 3.x Upgrade

**Input**: Design documents from `specs/007-stack-refresh/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened, traced platform
(002/004/005/006). Refreshes versions only — no lifecycle/UI/API change.

**Tests**: Re-run the full 001–006 integration suite per tier on the target machine before the next.
Task IDs continue the shared space (T118+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — grilled & refined, ready to build.** Scope: **broad stack refresh**
> (MLflow 3.x + image pinning + gateway/UI/native minor bumps), GPU/FT stack **frozen**. No constitution
> amendment (advances Principle VI). Tasks T118–T131.
>
> **Verified pre-flight (2026-06-28):** latest on PyPI — MLflow `3.14.0` + `mlflow-skinny 3.14.0` (both
> exist); FastAPI `0.138.1`, uvicorn `0.49.0`, pydantic `2.13.4`, boto3 `1.43.36`, prometheus-client
> `0.25.0`, httpx `0.28.1` (current). npm — Next latest `16.2.9` (we stay on **15.x**), React `19.2.7`.
> Verified in the 3.14 source: `start_span_no_context(name, inputs, attributes, start_time_ns,
> experiment_id) -> LiveSpan` + `span.end(end_time_ns=…)` — the clean fire-and-forget fit for FR-057.
>
> **Grilled decisions (2026-06-28):**
> 1. **Fresh `pgdata` volume + re-seed essentials** (NOT in-place `mlflow db upgrade`). Zero migration
>    risk; Kurt accepts losing MLflow run/trace history. **Datasets survive** (content-addressed on MinIO,
>    not Postgres). Re-seed = re-register + promote the serving LLM (`qwen2.5-7b` → `@serving`, so
>    `/infer` `registry_version` resolves) and run `scripts/seed_vision_model.py`. Old experiment IDs left
>    orphaned in the MinIO `mlflow` bucket (harmless). Fresh volume also sidesteps the Postgres-password
>    rotation gotcha. No pgdata snapshot needed.
> 2. **Refactor 006 tracing** to `mlflow.start_span_no_context` + `span.end(end_time_ns=…)` (non-deprecated
>    3.x API), behavior identical.
> 3. **Image pins = specific version tags** (e.g. `postgres:17.x-alpine`, `minio:RELEASE.…`,
>    `prom/prometheus:v3.x`, `grafana:11.x`) — readable, reproducible-enough; NOT digests.
> 4. **Gateway base = `python:3.12-slim`** (mature wheel coverage), NOT 3.13.
> 5. **MLflow target = `3.14.0`** (latest; fresh+reseed removes migration risk).
> 6. **Keep US1 and US3 gateway rebuilds SEPARATE** — US1 bumps only `mlflow-skinny`→3.x + re-validates;
>    US3 later bumps Python 3.12 + pip deps + re-validates. Clean bisect over fewer rebuilds.
> 7. **GPU/FT stack FROZEN** (non-negotiable): torch/torchvision/transformers/peft/accelerate/datasets
>    unchanged.

---

## Phase 0 — Pre-flight (gates everything)

- [ ] **T118** [US1] Confirm `mlflow==3.14.0` and `mlflow-skinny==3.14.0` resolve/install clean in the
  gateway + mlflow images (dry build). Capture the CURRENT validated image versions (`docker compose
  images`) so US2 can pin them. *(Grilled: fresh-volume migration — NO pgdata snapshot needed; the
  MLflow run/trace history loss is accepted, datasets survive on MinIO.)* (FR-055)

## Phase 1 — MLflow 2.18 → 3.x (US1, P1) → SC-036 + SC-038

- [ ] **T119** [US1] Bump MLflow to `3.14.0` everywhere, same version: `infra/mlflow/Dockerfile`
  (`mlflow==3.14.0` + psycopg2/boto3 minors), `gateway/requirements.txt` + `training/requirements.txt`
  (`mlflow-skinny==3.14.0`). Rebuild the gateway + mlflow images. *(Keep this gateway rebuild SEPARATE
  from US3's Python/dep bump — grilled.)* (FR-055)
- [ ] **T120** [US1] **Fresh backend** (grilled): drop the `pgdata` volume so the 3.x server inits a clean
  store (sidesteps the schema migration + the Postgres-password rotation gotcha). **Re-seed essentials**
  so the platform resolves again: re-register + promote the serving LLM (`qwen2.5-7b` → `@serving`) and
  run `scripts/seed_vision_model.py`. Datasets need no re-seed (content-addressed on MinIO). Old
  experiment artifacts in the MinIO `mlflow` bucket are left orphaned (harmless). (FR-055)
- [ ] **T121** [US1] Port `gateway/app/tracing.py` to the non-deprecated 3.x tracing API — verified shape:
  `span = mlflow.start_span_no_context(name, inputs=…, attributes=…, start_time_ns=…, experiment_id=…)`
  then `span.end(end_time_ns=…)` (set outputs/status on the span). Preserve fire-and-forget, fail-open,
  span-outside-`_gpu_lock`, frame-count, lazy worker-thread init, and the
  `MLFLOW_TRACING_ENABLED`/`MLFLOW_TRACE_CAPTURE_IO` toggles + container passthrough exactly. (FR-057)
- [ ] **T122** [P] [US1] Re-validate on the 3.x server: `test_serving` / `test_registry` /
  `test_datasets` / `test_finetune` / `test_drift_loop` + `test_tracing_rest` / `test_tracing_stream` /
  `test_tracing_resilience`; confirm history preserved and traces land via the 3.x API. (SC-036, SC-038)

## Phase 2 — Pin floating images (US2, P1) → SC-037

- [ ] **T123** [US2] `docker-compose.yml`: replace `:latest` on `minio/minio`, `minio/mc`,
  `prom/prometheus`, `grafana/grafana` (and pin the Postgres minor) with the validated versions captured
  at T118 / after a clean bring-up. Note each pin's source in a comment. (FR-056)
- [ ] **T124** [P] [US2] Clean `up_all`; `test_foundation` + `test_exposure` green (pins are healthy +
  loopback-bound). (SC-037)

## Phase 3 — Gateway Python refresh (US3, P2) → SC-039

- [ ] **T125** [US3] `gateway/Dockerfile` → `python:3.12-slim`; `gateway/requirements.txt` bump FastAPI
  (`0.138.x`), uvicorn (`0.49.x`), pydantic (`2.13.x`), boto3 (`1.43.x`), prometheus-client (`0.25.x`)
  (httpx already current). Rebuild. (FR-058)
- [ ] **T126** [P] [US3] Gateway suite green (auth/serving/registry/datasets/monitor/vision/stream/
  tracing); OpenAPI contract + `/metrics` output unchanged. (SC-039)

## Phase 4 — UI refresh, stay on Next 15 (US4, P3) → SC-040

- [ ] **T127** [US4] `ui/package.json` → latest **Next 15.x** + React `19.2.x` + tooling (TypeScript,
  Tailwind, PostCSS, autoprefixer, `@types/*`); `npm install` to refresh `package-lock.json`;
  `next build`; bounce the `ui` daemon (`pkill -f '[n]ext-server'`, supervisor restarts). NOT Next 16. (FR-059)
- [ ] **T128** [P] [US4] `test_ui_security` / `test_ui_smoke` / `test_ui_resilience` green; six tabs +
  BFF contract (allowlist, origin guard, `[::1]`, non-leaky errors, key absent from payloads) unchanged. (SC-040)

## Phase 5 — Safe native (non-GPU) bumps (US5, P3) → SC-041

- [ ] **T129** [US5] `serving/bento/requirements.txt` (BentoML, Pillow) + `training/requirements.txt`
  (Prefect) minor bumps; update `scripts/native_env.lock`. **torch / torchvision / transformers / peft /
  accelerate / datasets UNCHANGED** (frozen cu128 stack). (FR-060)
- [ ] **T130** [P] [US5] `test_bento` (CPU vision) + `test_finetune` + `test_drift_loop` green on the
  frozen torch stack (GPU pipeline intact). (SC-041)

## Phase 6 — Cross-cutting regression

- [ ] **T131** Full 001–006 keyed sweep green with the refreshed stack; GPU-lock hold time + inference
  latency unchanged; `native_env.lock` + `package-lock.json` + image pins committed. (SC-041)

---

## Dependencies & Execution Order

- **T118 (snapshot) gates everything** — never migrate the MLflow backend without a rollback point.
- **US1 (MLflow, T119–T122)** is the highest-risk; do it first behind the snapshot so a regression is
  isolated before the cheaper refreshes pile on.
- **US2 (image pins)** is independent and low-risk; **US3/US4/US5** are independent refresh tiers, each
  re-validated cumulatively.
- **T131 lands last** (needs every tier in place).

### Constitution gates (re-check each phase)
- Principle II untouched: GPU/FT stack frozen (verify no torch-family movement in US3/US5).
- Principle VI strengthened: pinned images + preserved MLflow history.
- No new runtime → no amendment (MLflow/Python/Node all pre-existing).

## Implementation Strategy

1. **Snapshot, then MLflow 3.x** → migrate + port tracing + re-validate. **Stop and validate.**
2. **Pin images** → clean bring-up.
3. **Gateway → UI → native** minor refreshes, each behind its own test gate.
4. Each phase re-runs the relevant 001–006 tests on the target machine; never regress; never move the
   frozen GPU stack.

## Out of Scope (recorded)
- **GPU/FT stack upgrade** (torch/transformers/…): frozen (FR-060) — a future increment with its own GPU
  re-validation.
- **Next.js 16**: deferred; 007 stays on Next 15 (FR-059).
- **MLflow 3.x feature adoption** (GenAI eval, prompt registry, logged-models workflows): version move
  only; features are a later increment.
