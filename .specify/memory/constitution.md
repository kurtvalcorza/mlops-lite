# MLOps-Lite Constitution

A self-hosted, full-lifecycle MLOps platform that runs entirely on a single developer
machine. This constitution governs every specification, plan, and implementation task.

## Core Principles

### I. Local-First, Single-Machine
The entire platform MUST run on one machine with no required cloud dependency. All services
run as containers under a single Docker Compose stack. Any feature that cannot function
fully offline (after initial image/model pulls) is out of scope. No managed cloud services,
no Kubernetes, no multi-node assumptions.

### II. Single-GPU, On-Demand Serving (NON-NEGOTIABLE)
At most ONE GPU tenant may be resident in GPU VRAM at any instant — any GPU-resident modality (LLM,
vision, ASR, …) **or** a training run — enforced by a **single, race-free GPU lease**: a
single-slot admission mechanism with no time-of-check/time-of-use window, so two callers can never both
proceed onto the GPU. CPU-only models (e.g. embeddings, tabular) hold no lease and are exempt. Tenants load on
request and release VRAM after use (idle-release); a **serving** tenant may additionally be released by an
**operator-confirmed preemptive swap** (evict the holder → free → load the target, strictly sequential — still
one tenant at a time), while a **running training/HPO/batch job is never preempted**; workers are not always-on. Admission is checked
against **live free VRAM** and no feature may assume more VRAM than the host GPU provides (`VRAM_GB` in
[hardware-profile.md](./hardware-profile.md)). This single-tenant GPU lease is the core constraint that
separates this platform from production cluster designs — violating it defeats the project's purpose.

### III. Lightweight Footprint
Idle infrastructure (databases, registry, broker, dashboards) MUST stay within ~3 GB RAM.
Disk is the scarcest resource on the target machine: prefer small/quantized models, cap the
model zoo, and prune unused images. Every added component must justify its resident cost
against this principle.

### IV. Full Lifecycle Coverage
The platform is an MLOps *platform*, not a model server. It MUST span the full lifecycle:
data versioning → training/fine-tuning → model registry → serving/inference → monitoring,
closed by a feedback loop (drift or new data triggers retraining). Dropping a stage requires
an explicit amendment.

### V. Open-Source & Swappable Components
Each lifecycle stage is backed by a mainstream open-source tool behind a clear interface, so
any one can be replaced without rewriting the others. Default stack: MinIO (storage), DVC
(data versioning), MLflow (tracking + registry), Prefect (orchestration), PyTorch + PEFT/LoRA
(training), Ollama + BentoML behind a FastAPI gateway (serving), Evidently + Prometheus/Grafana
(monitoring). Replacements are allowed; lock-in is not.

### VI. Reproducibility & Observability
Every experiment and model version MUST be tracked in MLflow; every dataset version recorded
via DVC; every service must expose health and metrics. A run must be reproducible from its
recorded configuration. If it isn't tracked, it didn't happen.

### VII. Incremental, Phase-Gated Delivery
Work ships in independently-runnable phases — (1) infra + registry, (2) serving, (3)
orchestration + training, (4) monitoring + feedback loop. Each phase MUST be verifiable on the
target hardware (including real GPU passthrough) before the next begins. No big-bang builds.

## Hardware & Resource Budget

The platform targets the single machine defined in [hardware-profile.md](./hardware-profile.md)
and MUST remain comfortable within it. All limits are expressed relative to that profile:

- **GPU**: one live model in VRAM at a time, sized to fit `VRAM_GB`.
- **CPU / RAM**: idle infrastructure ≤ ~3 GB RAM, well within `RAM_GB`; +2–6 GB when active.
- **Disk**: treat `FREE_DISK_GB` as scarce — budget ~15 GB models + ~10 GB images and prune
  aggressively; the container data-root may be relocated to roomier storage if needed.

Any requirement that breaches the VRAM, RAM, or disk budget in the active profile is a
constitution violation and must be re-scoped or formally amended.

## Development Workflow

- **Spec-Driven Development** via Spec Kit: constitution → `/speckit-specify` → `/speckit-plan`
  → `/speckit-tasks` → `/speckit-implement`. Specs precede code.
- **Docker Compose** is the orchestration surface for all CPU/infra services. **GPU-bound
  services** (model serving, training) MAY run as native host processes (e.g., on the WSL GPU
  host) when the container engine cannot pass the GPU through (Amendment, 2026-06-27). No other
  runtime is introduced without amendment.
- **Node.js runtime (Amendment, 2026-06-28):** A Node.js runtime MAY be used **solely for the
  operator UI and its BFF**. The platform remains Python + Docker Compose for every other service;
  Node is confined to `ui/`.
- **Native non-GPU service (Amendment, 2026-06-28):** A non-GPU service MAY run as a native host
  process **when justified by disk-frugality (Principle III) and bound to localhost** — extending the
  GPU-only native-host allowance above. The operator UI runs natively in WSL on this basis.
  **Principle II (one model in VRAM) is unchanged.** No general "any web service" allowance.
- **GPU access is gate zero**: `nvidia-smi` must succeed in the GPU execution environment — a
  CUDA container where the engine supports it, otherwise the native WSL host — before any
  model-serving or training work proceeds.
- Each phase carries a quality checklist (`/speckit-checklist`) and must run end-to-end on the
  the target machine (per the hardware profile) before being marked done.

## Governance

This constitution supersedes ad-hoc technical choices. Any deviation — needing more than one
live model, exceeding the resource budget, introducing a cluster/orchestrator, or dropping a
lifecycle stage — requires a documented amendment with explicit justification before
implementation. Complexity must always be justified against Principles II and III. All plans
and task lists are reviewed for compliance with these principles.

**Version**: 1.4.1 | **Ratified**: 2026-06-27 | **Last Amended**: 2026-07-01

<!-- v1.1.0: genericized — machine-specific values extracted to hardware-profile.md; constraints
     now expressed relative to VRAM_GB / RAM_GB / FREE_DISK_GB.
     v1.2.0: hybrid GPU — when the container engine cannot pass the GPU through, GPU-bound
     services (serving, training) run natively on the WSL GPU host; Gate Zero accepts native
     nvidia-smi. All CPU/infra services remain in Docker Compose. Principle II (one model in
     VRAM at a time) is unchanged.
     v1.3.0: operator UI (003-frontend) — a Node.js runtime is permitted solely for the operator
     UI/BFF (confined to ui/); a non-GPU service (the UI) may run natively on the WSL host when
     justified by disk-frugality (Principle III) and bound to localhost, extending the GPU-only
     native-host allowance. No general "any web service" allowance. Principle II unchanged.
     v1.4.0: Principle II generalized from "one LLM model in VRAM + serving<->training mutex" to "one GPU
     tenant under a single race-free lease — any GPU-resident modality OR a training run; live-VRAM
     admission; CPU-only models exempt." A strengthening generalization; the rule stays NON-NEGOTIABLE.
     On-demand load + idle-release + VRAM budget retained.
     v1.4.1 (017-swap-on-demand): Principle II wording clarified — a *serving* tenant may also be released by
     an OPERATOR-CONFIRMED PREEMPTIVE SWAP (evict→free→load, strictly sequential), in addition to idle-release;
     a running training/HPO/batch job is NEVER preempted. This is a PATCH-level clarification, not a rule
     change: at most one GPU tenant resident at any instant is unchanged and still NON-NEGOTIABLE (008's
     earlier "cooperative, no swap/evict" *description* is superseded; the one-tenant invariant is not). -->

