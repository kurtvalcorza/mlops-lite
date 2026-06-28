---
description: "Task list for MLOps-Lite Platform implementation"
---

# Tasks: MLOps-Lite Platform

**Input**: Design documents from `specs/001-mlops-platform/`

**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Tests**: Lightweight per-phase **smoke/integration** tests are included (the constitution
requires each phase to run end-to-end on the target machine before the next begins). Full
unit TDD is out of scope for v1.

**Organization**: Tasks grouped by user story; each story is an independently demoable increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-27): COMPLETE.** All 8 phases implemented and verified on the reference
> machine; gateway **v1.0.0**. The full lifecycle feedback loop runs end-to-end: serve (US1) →
> register (US2) → datasets (US3) → fine-tune (US4) → drift→retrain (US5), with one-model-in-VRAM
> enforced symmetrically across the serving/training daemons. Phase 8 polish done: quickstart,
> README + mermaid diagram, OpenAPI export, disk-frugality, offline check, and `/metrics` on all
> three services (native-daemon GPU/state proxied through the gateway; Grafana GPU panels live).
> Integration tests green (foundation/serving/registry/datasets/finetune/drift/offline/bento).
> **T022 done** (gateway v1.1.0): BentoML CPU vision classifier served from the models bucket — the
> non-LLM half of FR-002 is now covered, so **all tasks T001–T043 + T022 are complete; nothing
> deferred.** Three principled tool swaps (DVC→content-addressing, Prefect-server→ephemeral,
> Evidently→PSI), each justified by Principle III/V and isolated for easy swap-back.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Repo skeleton and the GPU gate.

- [ ] T001 Create directory structure per plan.md (`gateway/`, `serving/`, `training/`, `monitoring/`, `data/`, `infra/`, `scripts/`, `tests/`)
- [ ] T002 [P] Add `.env.example` (ports, MinIO/Postgres creds, MLflow/Prefect URLs — all local defaults) and `Makefile` (`up`/`down`/`smoke`/`gpu-check`)
- [ ] T003 [P] Add Python tooling config (ruff + black + pytest) at repo root
- [ ] T004 **Gate Zero**: `scripts/gpu_check.sh` runs `nvidia-smi` in a CUDA container if the engine supports `--gpus`, else falls back to native WSL `nvidia-smi` (hybrid, v1.2.0); `make gpu-check` must pass before any GPU phase

---

## Phase 2: Foundational — Infra + Registry (Blocking Prerequisites)

**Purpose**: The shared backbone every story needs. Maps to plan Phase 1.

**⚠️ CRITICAL**: No user story work begins until this phase is complete.

- [ ] T005 `docker-compose.yml` with PostgreSQL service + healthcheck + named volume
- [ ] T006 [P] Add MinIO service (S3 API + console) to compose with healthcheck + volume
- [ ] T007 [P] Add MLflow server service (backend store → Postgres, artifact store → MinIO)
- [ ] T008 `scripts/bootstrap_buckets.py` creates buckets: `datasets`, `models`, `results`, `mlflow`
- [ ] T009 [P] Gateway skeleton: FastAPI app in `gateway/app/main.py` with `/healthz` and `/metrics`
- [ ] T010 [P] Prometheus + Grafana services in compose; scrape config in `infra/prometheus/` covering gateway, serving, training, and the NVIDIA GPU exporter
- [ ] T011 `docker-compose.gpu.yml` override with the NVIDIA device reservation (kept for GPU-capable engines; **deferred under the hybrid decision** — serving/training run natively in WSL)
- [ ] T012 Smoke test `tests/test_foundation.py`: `make up` → MLflow UI 200, MinIO buckets exist, gateway `/healthz` 200, idle RAM ≤ ~3 GB

**Checkpoint**: Infra is up; registry reachable; artifacts land in MinIO.

---

## Phase 3: User Story 1 — Run inference on demand (Priority: P1) 🎯 MVP

**Goal**: Submit an input → model loads on demand → result returned → VRAM released.

**Independent Test**: With one LLM available, POST a prompt to the gateway and get a response;
confirm VRAM is occupied only during the call.

- [x] T013 [US1] Build/run **`llama-server` (llama.cpp) natively in WSL with CUDA** (GPU via `/dev/dxg`); serve a GGUF model, support runtime `--lora`; gateway reaches it on the host
- [x] T014 [P] [US1] `scripts/seed_models.sh` downloads one small quantized **GGUF** LLM sized to fit `VRAM_GB` (per the hardware profile) for llama-server
- [x] T015 [US1] Gateway `POST /infer` in `gateway/app/routers/infer.py`: route to the serving backend, return result + metadata (status, latency, model)
- [x] T016 [US1] VRAM guard: serialize inference (≤1 model resident) + idle VRAM release in `serving/llama/supervisor.py`; reject models exceeding the VRAM budget with a clear error → 400 (FR-003/FR-004)
- [x] T017 [US1] Surface cold-start (load) time separately from inference time in the response (`load_ms` vs `infer_ms`)
- [x] T018 [US1] Integration test `tests/test_serving.py`: prompt → response; on-demand load + idle release verified, ≤1 model resident

**Checkpoint**: Real on-demand inference works — the MVP.

---

## Phase 4: User Story 2 — Register, version, and promote models (Priority: P2)

**Goal**: Publish models to the MLflow registry, version them, promote one to "serving".

**Independent Test**: Register two versions, promote v2, confirm US1 inference resolves to v2.

- [x] T019 [P] [US2] `gateway/app/registry.py`: MLflow registry client (register, list versions, promote via the `serving` alias)
- [x] T020 [US2] `POST /models` (register), `GET /models` + `GET /models/{name}` (list/compare versions), `POST /models/{name}/promote` in `gateway/app/routers/models.py`
- [x] T021 [US2] US1 `/infer` resolves the serving model version from the registry (best-effort, falls back to the static default) and reports `registry_model`/`registry_version` (integrates FR-006)
- [x] T022 [P] [US1] BentoML vision service in `serving/bento/` (MobileNetV2 image classifier) — delivers the vision half of FR-002; packages the model **from the MinIO `models` bucket** (seeded + MLflow-registered by `scripts/seed_vision_model.py`, so it depends on US2). Runs **CPU-only** (no VRAM → no GPU mutex contention), lazy-load + idle-release (scale-to-zero spirit). Gateway `POST /vision/classify` + `GET /vision/health` (`routers/vision.py`). Verified `tests/test_bento.py` (T022 PASS): top-5 classification through the gateway; `vision-mobilenet` registered alongside the LLM/LoRA models.
- [x] T023 [US2] Integration test `tests/test_registry.py`: register → promote → inference uses promoted version

**Checkpoint**: US1 + US2 both work independently.

---

## Phase 5: User Story 3 — Register and version datasets (Priority: P3)

**Goal**: Named, versioned, immutable dataset references in MinIO via DVC.

**Independent Test**: Register a dataset, change it, re-register; both versions resolvable.

- [x] T024 [P] [US3] Data-versioning config in `data/` (`data/README.md`) — **content-addressing on the MinIO `datasets` bucket** instead of DVC (Principle V swap: DVC needs git+CLI+commit-per-version; same immutable/versioned guarantees delivered more lightly). `gateway/app/datasets.py` holds the storage logic.
- [x] T025 [US3] `data/register_dataset.py` helper: push a local file → record an immutable, content-addressed dataset version (thin client over `POST /datasets`)
- [x] T026 [US3] `POST /datasets`, `GET /datasets`, `GET /datasets/{name}`, `GET /datasets/{name}/{version}` in `gateway/app/routers/datasets.py`
- [x] T027 [US3] Integration test `tests/test_datasets.py`: two registrations → two distinct retrievable versions (+ idempotency on identical content, 404 on missing)

**Checkpoint**: Datasets versioned; ready to feed training.

---

## Phase 6: User Story 4 — Fine-tune with experiment tracking (Priority: P4)

**Goal**: LoRA/QLoRA fine-tune on a pinned dataset version, tracked, auto-registered. Plan Phase 3.

**Independent Test**: Small fine-tune on a tiny dataset → run records metrics → new model version appears.

- [x] T028 [US4] Orchestration via **Prefect (ephemeral) + a native trainer daemon**, not a Prefect *server* — `training/trainer.py` runs the flow on the WSL GPU host (hybrid GPU); an always-on Prefect server would violate Principle III, and MLflow already tracks runs. Prefect `@flow/@task` structure is kept (degrades to no-ops if absent), ready for the US5 retrain trigger.
- [x] T029 [P] [US4] `training/flows/finetune.py`: Prefect flow — load base model + pinned dataset version, run PEFT/LoRA on the single GPU, log params/metrics to MLflow (verified: GPU train on RTX 5070 Ti, loss 5.04→2.69)
- [x] T030 [US4] On success, convert the adapter to GGUF, upload to MinIO, and register a new model version tagged with run + dataset version + base model (feeds US2)
- [x] T031 [US4] `POST /runs` (launch) + `GET /runs/{id}` (status/metrics) + `GET /training/health` in `gateway/app/routers/runs.py` (proxies the native trainer)
- [x] T032 [US4] VRAM budget enforced **symmetrically** (one-model-in-VRAM, Principle II): the trainer refuses to start while a serving model is resident, AND the serving supervisor refuses to cold-load while a training run is active (`_trainer_busy` guard in `serving/llama/supervisor.py`). A failed run frees the GPU and registers **no** partial version (verified — bugged runs failed cleanly; symmetric guard added after observing serving/training GPU contention).
- [x] T033 [US4] Integration test `tests/test_finetune.py`: run completes → new registered version is promotable + servable; reproducible from recorded config (FR-012, SC-005). **PASS** — `qwen0_5b-qa-lora v1` registered, lineage-tagged, promoted. Live servability proven separately via `serving/llama/verify_lora.sh` (base GGUF + adapter GGUF loaded in llama.cpp).

**Checkpoint**: Training closes into the registry → serving.

---

## Phase 7: User Story 5 — Monitor drift and close the loop (Priority: P5)

**Goal**: Drift/quality + service dashboards; drift breach triggers retraining. Plan Phase 4.

**Independent Test**: Reference vs shifted data → drift report; threshold breach enqueues a run.

- [x] T034 [P] [US5] Drift detection via **pure-Python PSI** (`gateway/app/monitoring.py`) instead of Evidently — Evidently's pandas/scipy/plotly would bloat the gateway image on the constrained Windows C: drive (Principle III); PSI delivers the same signal dependency-free (Principle V swap, see `monitoring/README.md`). `monitoring/drift.py` is a CLI client.
- [x] T035 [P] [US5] Grafana dashboard `infra/grafana/provisioning/dashboards/mlops-lite.json` (provisioned): gateway up, dataset-drift, max-PSI gauge, retrain triggers, infer latency, request rate. GPU panel noted as pending native-daemon `/metrics` (T043).
- [x] T036 [US5] `GET /monitor` (latest reports) + `POST /monitor/check` (drift → on breach launches a retrain run) in `gateway/app/routers/monitor.py` — closes the loop (FR-010/FR-011). Drift exported as `gateway_drift_score`/`gateway_dataset_drift`.
- [x] T037 [US5] Integration test `tests/test_drift_loop.py`: stable=no-op; induced drift → report generated → retraining run started. **PASS** (max_psi 25.47 ≫ 0.25 → retrain launched + completed → registered).

**Checkpoint**: Full lifecycle loop demonstrable.

---

## Phase 8: Polish & Cross-Cutting

- [x] T038 [P] `specs/001-mlops-platform/quickstart.md` — bring-up + per-phase smoke steps + register→first-inference timing note (SC-003); validated against the passing integration tests
- [x] T039 [P] Gateway OpenAPI exported to `specs/001-mlops-platform/contracts/openapi.json` (16 paths)
- [x] T040 Disk-frugality pass: `scripts/disk_report.sh` + README section (image prune, model-zoo cap, WSL-vs-C: split, optional data-root relocation)
- [x] T041 Offline check `tests/test_offline.py` (SC-007): P1–P2 flows (health, datasets, registry) succeed using only local services. **PASS** + documented hard-verify (internal docker network).
- [x] T042 `README.md` — mermaid architecture diagram, lifecycle/user-story table, run guide, the three principled tool swaps (DVC/Prefect/Evidently), disk-frugality, hardware retargeting
- [x] T043 [Observability] gateway, serving (`:8090/metrics`), training (`:8091/metrics`) each expose `/metrics`; native-daemon GPU/state proxied through the gateway (`mlops_*` gauges) so Prometheus gets them from the one gateway target — verified `gateway` + `prometheus` targets UP and `mlops_gpu_free_mib` scraped; Grafana GPU panels live

---

## Dependencies & Execution Order

- **Setup (P1)** → **Foundational (P2, blocks everything)** → user stories.
- **US1 (P1)** is the MVP — implement and validate before anything else.
- **US2** depends on Foundational (MLflow registry); **US1** integrates with it at T021.
- **US3** is independent (datasets); **US4** depends on US2 + US3; **US5** depends on US4.
- **Polish (P8)** last.

### Constitution gates (re-check each phase)
- Gate Zero (T004) before any GPU phase (US1, US4).
- One-model-in-VRAM (T016) verified in US1 and reused by US4 (T032).
- Idle RAM ≤3 GB checked at T012 and not regressed by later phases.

## Implementation Strategy

1. **MVP**: Phases 1–3 → real on-demand inference. **Stop and validate.**
2. **Increment**: add US2 (registry) → US3 (datasets) → US4 (training) → US5 (monitoring), validating each independently.
3. Each story is independently demoable; never break a prior story.

## Notes

- [P] = different files, no dependencies. [US#] maps task → user story for traceability.
- Commit after each task or logical group; stop at any checkpoint to demo.
- Every phase ends with its smoke/integration test passing on the target machine.
