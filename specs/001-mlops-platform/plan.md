# Implementation Plan: MLOps-Lite Platform

**Branch**: `001-mlops-platform` | **Date**: 2026-06-27 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-mlops-platform/spec.md`

## Summary

Deliver a full-lifecycle MLOps platform as a single Docker Compose stack on one machine. A
FastAPI **gateway** fronts on-demand model serving (Ollama for LLMs, BentoML for vision/audio),
backed by **MLflow** (experiment tracking + model registry on PostgreSQL + MinIO), **MinIO** +
**DVC** for data/artifacts, **Prefect** for orchestrating training/fine-tuning (PyTorch + PEFT/
LoRA), and **Evidently + Prometheus/Grafana** for monitoring and the drift→retrain loop. The
single GPU hosts at most one model at a time, loaded on demand. Built in four phase-gated slices.

## Technical Context

**Language/Version**: Python 3.11+ (gateway, serving adapters, training flows); YAML for Compose.

**Primary Dependencies**: FastAPI + Uvicorn (gateway); Ollama (LLM serving); BentoML (vision/
audio serving); MLflow (tracking + registry); Prefect (orchestration); PyTorch + Transformers +
PEFT (training/LoRA); boto3 / MinIO SDK; DVC (data versioning); Evidently (drift); Prometheus +
Grafana + NVIDIA DCGM/`nvidia-smi` exporter (metrics).

**Storage**: PostgreSQL (MLflow backend store + gateway metadata); MinIO (S3-compatible: datasets,
model weights, artifacts, results). Redis optional (deferred — Prefect provides job orchestration).

**Testing**: pytest (unit + integration); Compose-level smoke tests per phase; container GPU smoke
test (`nvidia-smi` inside a CUDA container) as gate zero.

**Target Platform**: Docker Compose on any compatible engine (Docker Desktop, Rancher, etc.),
Linux or Windows + WSL2, single machine, NVIDIA GPU with CUDA passthrough. Concrete values for
the active machine live in `.specify/memory/hardware-profile.md`.

**Project Type**: Multi-service web platform (compose stack of infra + application services).

**Performance Goals**: idle infra ≤ ~3 GB RAM; at most one model resident within `VRAM_GB`; model
cold-start (load) reported separately from inference latency.

**Constraints**: one live model within `VRAM_GB`; ≤ ~3 GB idle RAM; disk-frugal within `FREE_DISK_GB`;
offline-capable after initial pulls; single GPU; single local operator.

**Scale/Scope**: one operator, up to ~5 small/quantized models in the local zoo, single target machine (per the hardware profile).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after design.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | One Compose stack, no cloud service, offline after pulls | ✅ by design (FR-013/014) |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | Serving loads ≤1 model in VRAM; Ollama `keep_alive=0` / BentoML scale-to-zero; gateway serializes requests | ✅ enforced in serving layer (FR-003/004) |
| III. Lightweight Footprint | Infra budgeted ≤3 GB idle; Redis deferred; model zoo capped | ✅ (SC-001) |
| IV. Full Lifecycle Coverage | Data → train → registry → serve → monitor + loop all in scope | ✅ US1–US5 |
| V. OSS & Swappable | Each stage behind a clear interface; mainstream OSS | ✅ |
| VI. Reproducibility & Observability | MLflow + DVC + per-service metrics | ✅ (FR-012/015) |
| VII. Phase-Gated Delivery | 4 independently-runnable phases | ✅ (see Tasks) |
| Gate Zero: GPU access | `nvidia-smi` must pass in the GPU env (container where supported, else native WSL — hybrid, v1.2.0) | ✅ T004 |

No violations requiring justification (see Complexity Tracking).

## Project Structure

### Documentation (this feature)

```text
specs/001-mlops-platform/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 — tool/version decisions (present)
├── data-model.md        # Phase 1 — entities & relationships (present)
├── quickstart.md        # Phase 1 — bring-up & smoke steps (built in T038)
├── contracts/           # Phase 1 — gateway OpenAPI (built in T039)
└── tasks.md             # Phase 2 — task list (/speckit-tasks)
```

### Source Code (repository root)

```text
mlops-lite/
├── docker-compose.yml          # core infra + app services
├── docker-compose.gpu.yml      # NVIDIA runtime overrides for serving/training
├── .env.example                # credentials, ports, paths (all local defaults)
├── Makefile                    # up / down / smoke / gpu-check
├── infra/                      # service configs
│   ├── postgres/               # init for MLflow + gateway DBs
│   ├── minio/                  # bucket bootstrap (datasets, models, results, mlflow)
│   ├── mlflow/                 # MLflow server image/config
│   ├── prefect/                # Prefect server + worker config
│   ├── prometheus/             # scrape config (services + GPU exporter)
│   └── grafana/                # dashboards (service health, GPU, drift)
├── gateway/                    # FastAPI gateway (single entry point)
│   ├── app/                    # routers: /infer, /models, /datasets, /runs, /monitor
│   └── tests/
├── serving/
│   ├── ollama/                 # LLM serving (keep_alive=0)
│   └── bento/                  # BentoML services for vision/audio, scale-to-zero
├── training/
│   └── flows/                  # Prefect flows: LoRA/QLoRA fine-tune → log → register
├── monitoring/                 # Evidently jobs + drift→retrain trigger
├── data/                       # DVC config + dataset-registration helpers
├── scripts/                    # gpu_check.sh, bootstrap_buckets.py, seed_models.sh
└── tests/                      # cross-service integration & smoke tests
```

**Structure Decision**: Single multi-service repo orchestrated by one Compose file (plus a GPU
override file). Application code is Python services (`gateway`, `serving`, `training`,
`monitoring`); infra is configuration only. This matches Principle I (one stack) and keeps each
swappable component (Principle V) in its own directory behind the gateway's interface.

## Phasing (maps to constitution VII)

- **Phase 1 — Infra + Registry**: Compose with Postgres + MinIO + MLflow; bucket bootstrap;
  health checks. Exit: MLflow UI reachable, artifacts land in MinIO.
- **Phase 2 — Serving (GPU)**: gateway `/infer` + Ollama running **natively on the WSL GPU host**
  (LLM) + one BentoML service; on-demand load, one-model-in-VRAM enforcement. Exit: real
  inference, VRAM occupied only during calls. (Hybrid GPU per constitution v1.2.0.)
- **Phase 3 — Orchestration + Training**: Prefect + a LoRA fine-tune flow logging to MLflow and
  auto-registering the output. Exit: a run produces a new servable model version.
- **Phase 4 — Monitoring + Loop**: Evidently drift reports + Grafana dashboards + drift→retrain
  trigger. Exit: threshold breach starts a retraining run end-to-end.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Two serving tools (Ollama + BentoML) | Ollama gives effortless quantized-LLM hot-swap; BentoML packages arbitrary vision/audio models from the registry | A single tool covers one modality well but not both within the lightweight budget |
| Prefect for orchestration | Need tracked, retriggerable training runs and the drift→retrain loop | Plain scripts/cron can't model run state, retries, or the feedback trigger cleanly |
| Redis deferred, not included | Avoid an extra always-on service (Principle III) | Prefect already provides job orchestration; add Redis only if a real async queue need appears |
