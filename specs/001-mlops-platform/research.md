# Research & Decisions (Phase 0): MLOps-Lite Platform

Tooling decisions for each lifecycle stage, judged against the constitution (Local-First,
Single-GPU On-Demand, Lightweight, OSS & Swappable). Concrete machine values come from
`.specify/memory/hardware-profile.md`.

| Stage | Decision | Rationale (vs. constitution) | Alternatives rejected |
|---|---|---|---|
| Orchestration runtime | **Docker Compose** (one `docker-compose.yml` + `docker-compose.gpu.yml`) | Principle I — one stack, no cluster tax | Kubernetes/k3s (violates III, the whole reason we left DIMER) |
| Object storage | **MinIO** | S3-compatible, ~200 MB, local | Cloud S3 (violates I) |
| Data versioning | **DVC** with MinIO remote | Git-style pointers, no resident service | LakeFS (heavier server) |
| Tracking + registry | **MLflow** (backend → PostgreSQL, artifacts → MinIO) | One tool does experiments + registry; light | W&B (cloud), separate registry service |
| Relational store | **PostgreSQL** | MLflow backend + gateway metadata in one DB | SQLite (weak concurrency), two DBs (heavier) |
| Orchestration | **Prefect** (server + worker) | Python-native, tracked retriable runs + drift→retrain trigger | Airflow/KubeFlow (heavy), cron (no run state) |
| LLM serving | **llama.cpp `llama-server`** (CUDA, native WSL) | Most VRAM-frugal GGUF engine; runtime `--lora` to serve fine-tuned adapters; manual VRAM lifecycle | Ollama (llama.cpp wrapper, weaker LoRA/control); vLLM (resident, heavy, throughput-oriented) |
| Vision/audio serving | **BentoML**, scale-to-zero | Packages arbitrary models from the registry; idles to zero VRAM | TorchServe/Triton (heavier footprint) |
| API gateway | **FastAPI + Uvicorn** | Single entry point; serializes inference to enforce one-in-VRAM | Django (heavier; DIMER's choice) |
| Training | **PyTorch + Transformers + PEFT (LoRA/QLoRA)** | LoRA fits a ≤8B QLoRA in `VRAM_GB`; trains an adapter, not the full model | Full fine-tune (exceeds VRAM) |
| Drift/quality | **Evidently** | Python lib, no resident service | Custom stats (reinventing) |
| Metrics/dashboards | **Prometheus + Grafana** + NVIDIA GPU exporter | Standard, light; GPU + service health + drift panels | Cloud observability (violates I) |
| Async queue | **Deferred (none)** | Prefect already orchestrates; avoid an extra always-on service (Principle III) | Redis/RabbitMQ now (premature) |

## Key open decisions resolved
- **One-model-in-VRAM enforcement**: combination of the `llama-server` lifecycle (idle unload /
  start-stop), BentoML scale-to-zero, and a gateway-level serialization guard
  (`gateway/app/serving.py`, T016). The gateway is the single authority that prevents two models
  loading at once.
- **GPU passthrough (Gate Zero)**: verified by running `nvidia-smi` inside a CUDA container
  (`scripts/gpu_check.sh`, T004) before any serving/training service starts. On Windows hosts this
  relies on the WSL2 NVIDIA runtime; on Linux, the NVIDIA Container Toolkit.
- **Model size policy**: registry rejects any `ModelVersion` whose footprint exceeds `VRAM_GB`
  headroom (≈`VRAM_GB − 1`); default zoo capped at ~5 models for disk (`FREE_DISK_GB`).

## Risks / watch-items
- **Disk** (`FREE_DISK_GB`) is the tightest budget — prune images, cap the zoo, optionally relocate
  the container data-root (T040).
- BentoML + llama-server both touching the GPU must never overlap — the gateway guard is the safeguard.
- First-call cold-start (model load) latency is inherent to on-demand serving; surfaced separately
  (T017) so it isn't mistaken for slow inference.
