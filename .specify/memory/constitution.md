<!--
SYNC IMPACT REPORT — Constitution amendment
Version: 1.5.2 → 1.6.0 (MINOR — co-residency generalization) → 1.6.1 (PATCH — corrected VRAM wording per Codex review)
Modified principles:
  - II. Single-GPU, On-Demand Serving (title unchanged) — core invariant generalized from
    "at most ONE GPU tenant resident at any instant" → "combined footprint of GPU-resident tenants MUST
    never exceed the live free VRAM budget," admitting BOUNDED CO-RESIDENCY of multiple SERVING tenants
    within VRAM_GB via the same single race-free admission authority. Training stays exclusive & never
    preempted; live-VRAM admission, CPU-only exemption, idle-release, operator-confirmed swap all retained.
Added sections: none
Removed sections: none
Also updated: Hardware & Resource Budget → GPU bullet (combined resident footprint ≤ VRAM_GB).
Templates checked:
  - .specify/templates/plan-template.md — ✅ no change (Constitution Check is generic; no hardcoded Principle II text)
  - .specify/templates/spec-template.md  — ✅ no change (generic)
  - .specify/templates/tasks-template.md — ✅ no change (generic)
Memory files:
  - .specify/memory/hardware-profile.md — ✅ UPDATED (PR #74 review). Principle II points here for the
    VRAM budget, so it is a dependent artifact and was missed by the original v1.6.0/v1.6.1 sync: it
    still asserted "Live models in VRAM: exactly 1 at a time," contradicting bounded co-residency.
    Now states the two-bound rule and defines the admission tunables (safety_reserve, safety_headroom,
    max_admission_attempts, drain_timeout, job_drain_timeout, admission_backoff) that the spec,
    plan, and contracts/admission-scheduler.md all reference.
Runtime guidance:
  - README.md — ⚠ PENDING: still describes the as-built SINGLE-TENANT system (≈ lines 56, 77, 287, 291).
    Accurate for today's implementation; update to VRAM-budget co-residency only when feature
    026-lan-gpu-broker implements it (do not rewrite now — would document unbuilt behavior).
Follow-up TODOs: implement 026-lan-gpu-broker co-residency, then refresh README's Principle II descriptions.
-->

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
The platform has exactly ONE GPU. The **combined footprint of all GPU-resident tenants MUST never exceed the
usable VRAM budget** — the GPU's total `VRAM_GB` (in [hardware-profile.md](./hardware-profile.md)) less a
safety reserve — and **each individual load MUST additionally fit within live free VRAM** (measured, with
headroom, and reconciled against the actual post-load delta) so estimation error can never over-commit the
device. (Note: "live free VRAM" already excludes current residents; it bounds the *incoming* load, while the
usable-budget sum bounds the *accounted set* — the two checks are distinct and both required.) This is
enforced by a **single, race-free admission authority**, since **018** realized by the GPU host agent's
**in-process admission**
(`hostagent/admission.py`): one re-entrant lock makes the holder-check and the claim a single critical section
— no time-of-check/time-of-use window and no cross-process lockfile to reclaim — so no set of callers can ever
admit past the live-VRAM budget. Within that budget, **multiple _serving_ tenants MAY be co-resident** (e.g.
several small models across modalities — LLM, vision, ASR — serving concurrently); a large model may consume
most of the budget and leave room for none. When a requested serving model does not fit alongside the current
serving tenants, admission MUST **evict** resident serving tenants by a defined policy (idle-first /
least-recently-used) to make room, or **refuse** with a clear reason if it cannot fit even alone. Tenants load
on request and release VRAM after use (idle-release); a serving tenant may additionally be released by an
**operator-confirmed preemptive swap**. **CPU-only models** (e.g. embeddings, tabular) hold no GPU lease and
are exempt.

**Training is exclusive and never preempted.** A **training / HPO / batch job MUST run with EXCLUSIVE
whole-GPU use** — no other tenant co-resident for the job's duration — and a **running training/HPO/batch job
is NEVER preempted**. Serving and training stay mutually exclusive: a job takes the whole GPU; workers are not
always-on. Admission is always checked against **live free VRAM** and no feature may assume more VRAM than the
host GPU provides. This VRAM-budgeted single-GPU admission — one authority, resident tenants never exceeding
live VRAM, training exclusive and never preempted — is the core constraint that separates this platform from
production cluster designs; violating it defeats the project's purpose.

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
any one can be replaced without rewriting the others. Default stack: Garage (S3 object
storage), content-addressed dataset versions on the object store
(data versioning), MLflow (tracking + registry), Prefect ephemeral runs (orchestration),
PyTorch + PEFT/LoRA (training), llama.cpp / whisper.cpp / slim FastAPI children behind the GPU
host agent and a FastAPI gateway (serving), hand-rolled PSI + quality windows with
Prometheus/Grafana (monitoring). Replacements are allowed; lock-in is not.

### VI. Reproducibility & Observability
Every experiment and model version MUST be tracked in MLflow; every dataset version recorded
in the content-addressed dataset registry on the object store; every service must expose
health and metrics. A run must be reproducible from its recorded configuration. If it isn't
tracked, it didn't happen.

### VII. Incremental, Phase-Gated Delivery
Work ships in independently-runnable phases — (1) infra + registry, (2) serving, (3)
orchestration + training, (4) monitoring + feedback loop. Each phase MUST be verifiable on the
target hardware (including real GPU passthrough) before the next begins. No big-bang builds.

## Hardware & Resource Budget

The platform targets the single machine defined in [hardware-profile.md](./hardware-profile.md)
and MUST remain comfortable within it. All limits are expressed relative to that profile:

- **GPU**: GPU-resident tenants sized so their **combined accounted footprint fits a usable budget**
  (`VRAM_GB` less a safety reserve), with **each load also fitting live free VRAM** (headroom + post-load
  reconciliation) — bounded co-residency for serving tenants; exclusive whole-GPU for a training/batch job.
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
  **Principle II is unchanged by this allowance.** No general "any web service" allowance.
- **GPU access is gate zero**: `nvidia-smi` must succeed in the GPU execution environment — a
  CUDA container where the engine supports it, otherwise the native WSL host — before any
  model-serving or training work proceeds.
- Each phase carries a quality checklist (`/speckit-checklist`) and must run end-to-end on the
  the target machine (per the hardware profile) before being marked done.

## Governance

This constitution supersedes ad-hoc technical choices. Any deviation — exceeding the live-VRAM
admission budget, running more than one GPU tenant during a training job, preempting a running job,
introducing a cluster/orchestrator, or dropping a lifecycle stage — requires a documented amendment with
explicit justification before implementation. Complexity must always be justified against Principles II
and III. All plans and task lists are reviewed for compliance with these principles.

**Version**: 1.6.1 | **Ratified**: 2026-06-27 | **Last Amended**: 2026-07-19

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
     earlier "cooperative, no swap/evict" *description* is superseded; the one-tenant invariant is not).
     v1.5.0 (018-platform-rearchitecture): Principle II mechanism sentence now names the lease's REALIZATION —
     the single race-free lease is the GPU host agent's **in-process admission** (`hostagent/admission.py`, one
     re-entrant lock guarding the holder-check + claim), superseding the pre-018 cross-process file lockfile
     (`serving/gpu_lease.py`, retired at T364). MINOR: the mechanism DESCRIPTION changed (cross-process lockfile
     → in-process lock); the RULE — at most one GPU tenant resident at any instant — is UNCHANGED and still
     NON-NEGOTIABLE.
     v1.5.1 (020-stack-remediation, T418): Principle V's ILLUSTRATIVE default-stack list refreshed to the
     real components after 020 — Garage replaced the archived-upstream MinIO (migrated + decommissioned,
     T404–T406); BentoML retired for slim FastAPI children behind the GPU host agent (T407–T412);
     DVC/Ollama/Evidently were long since realized by content-addressed datasets, llama.cpp, and the
     hand-rolled PSI/quality monitors. PATCH: wording-only — the rule ("behind a clear interface …
     Replacements are allowed; lock-in is not") is unchanged; 020 exercised it twice.
     v1.5.2 (023-platform-architecture-hardening, T548): Principle VI's stale "recorded via DVC" sentence
     corrected to the IMPLEMENTED authority — the content-addressed dataset registry on the object store —
     which Principle V has described since v1.5.1 (DVC was rejected at 001; see README §Default stack).
     PATCH: wording-only — the RULE (every dataset version recorded; if it isn't tracked, it didn't happen)
     is unchanged. Observed by 023's plan (§Constitution errata), processed as its own amendment rather than
     silently folded into a feature change.
     v1.6.0 (026-lan-gpu-broker): Principle II generalized from "at most ONE GPU tenant resident at any
     instant" to "the combined footprint of GPU-resident tenants MUST never exceed the live free VRAM budget"
     — admitting BOUNDED CO-RESIDENCY of multiple SERVING tenants within VRAM_GB through the same single
     race-free admission authority (hostagent/admission.py). PRESERVED unchanged: training/HPO/batch jobs take
     the WHOLE GPU (no co-residency for the job's duration) and are NEVER preempted; live-free-VRAM admission;
     CPU-only exemption; on-demand load + idle-release; operator-confirmed preemptive swap of a serving tenant.
     ADDED: when a serving model does not fit alongside current tenants, admission MUST evict resident serving
     tenants (idle-first/LRU) or refuse if it cannot fit even alone. MINOR: a material semantic generalization
     of a NON-NEGOTIABLE principle (parallels v1.4.0's one-LLM→one-tenant generalization), enabling concurrent
     small-model multimodal serving for the 026 LAN GPU broker. The single admission authority is unchanged and
     the principle stays NON-NEGOTIABLE. Rationale: a self-service multi-tenant broker on one GPU serves many
     small models far better when compatible models co-reside within the VRAM budget rather than swapping on
     every modality switch. README Principle II descriptions remain ⚠ pending until 026 implements co-residency.
     v1.6.1 (026-lan-gpu-broker, Codex architecture review): PATCH — corrected the v1.6.0 VRAM wording, which
     conflated two distinct checks. "Combined footprint ≤ live free VRAM" double-counted residents (live-free
     already excludes them). Reworded to the correct pair: the accounted resident set ≤ a USABLE budget
     (VRAM_GB − safety reserve), AND each incoming load ≤ live free VRAM (with headroom + post-load delta
     reconciliation). Rule unchanged in intent (never over-commit the one GPU); wording now implementable.
     No new runtime is introduced by this patch — the 026 job-sandbox runtime (gVisor/Kata) remains OUTSIDE the
     permitted runtimes and will require its OWN amendment IF/when a sandbox-feasibility spike proves it works
     on the target host (Development Workflow: "No other runtime is introduced without amendment"). -->
