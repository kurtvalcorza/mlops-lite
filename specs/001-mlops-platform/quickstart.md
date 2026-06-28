# MLOps-Lite — Quickstart

Bring up the platform and validate each phase end-to-end on one machine. Hardware specifics
(VRAM, RAM, disk) live in [`.specify/memory/hardware-profile.md`](../../.specify/memory/hardware-profile.md).

## Prerequisites

- A container engine (Docker Desktop / Rancher Desktop) with Compose.
- For GPU phases (serving, training): an NVIDIA GPU reachable from **WSL** (`nvidia-smi` works in
  the WSL distro). The GPU services run **natively in WSL** (hybrid GPU, constitution v1.2.0) —
  the container engine does not need an NVIDIA runtime.
- One-time GPU build/setup in WSL (see `serving/llama/` and `training/`): `llama-server` built with
  CUDA, a GGUF model under `~/models/gguf/`, and a training venv at `~/mlops-train`.

## 1. Foundational stack (Phases 1–2)

```bash
make up                      # or: docker compose up -d --build
python tests/test_foundation.py
```

Expect: MLflow UI on :5500, MinIO console on :9001 (buckets `datasets/models/results/mlflow`),
gateway `/healthz` on :8080, Prometheus :9090, Grafana :3001, idle RAM ≤ ~3 GB. **6/6 checks pass.**

## 2. On-demand GPU serving (Phase 3, US1)

```bash
bash serving/llama/run.sh        # WSL: starts the serving supervisor (model loads on 1st request)
./scripts/serve_up.ps1           # PowerShell: brings the stack up pointed at the WSL daemons
python tests/test_serving.py
```

Expect: `/infer` returns a completion; VRAM is occupied only during the call and released after
idle. Cold-start `load_ms` is reported separately from `infer_ms`.

## 3. Model registry (Phase 4, US2)

```bash
python tests/test_registry.py
```

Expect: register two versions → promote one to `serving` → `/infer` resolves the promoted version.

## 4. Datasets (Phase 5, US3)

```bash
python tests/test_datasets.py
# or register a file:  python data/register_dataset.py iris ./iris.csv --format csv
```

Expect: two registrations → two distinct, immutable, content-addressed versions.

## 5. Fine-tune loop (Phase 6, US4)

```bash
bash training/run.sh             # WSL: starts the native training daemon (needs ~/mlops-train venv)
python tests/test_finetune.py
```

Expect: a pinned dataset → GPU LoRA run (tracked in MLflow) → adapter converted to GGUF →
registered, promotable, and servable model version. Live-serve check:
`bash serving/llama/verify_lora.sh <adapter.gguf>`.

## 6. Drift → retrain loop (Phase 7, US5)

```bash
python tests/test_drift_loop.py
```

Expect: stable data → no-op; induced drift → PSI report (stored in MinIO `results`, visible at
`GET /monitor`) → a retraining run is launched automatically. Grafana → **MLOps-Lite Platform**
dashboard shows service health, drift score, GPU occupancy.

## Timing check (SC-003, target < 10 min)

From `make up` on a warm machine (images pulled, model + base cached): foundation ~30 s, first
inference (cold model load) a few seconds, register→promote→infer well under a minute. A first
fine-tune is dominated by the one-time base-model download; subsequent runs are seconds.

## One-model-in-VRAM (Principle II)

Serving and training **mutually exclude** on the single GPU: the trainer refuses to start while a
model is resident in serving, and serving refuses to cold-load while a training run is active. If
you hit a "GPU busy" error, let the other side finish (serving idle-releases after its timeout).

## Tear down

```bash
make down                    # stop the stack (volumes persist)
# WSL: stop the native daemons
pkill -f '[s]upervisor.py'; pkill -f '[t]rainer.py'
```
