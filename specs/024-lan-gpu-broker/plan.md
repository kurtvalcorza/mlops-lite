# Implementation Plan: LAN Self-Service GPU Broker

**Branch**: `024-lan-gpu-broker` | **Date**: 2026-07-19 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/024-lan-gpu-broker/spec.md`

## Summary

Turn the platform's existing single-authority GPU host agent into a **multi-tenant, self-service broker**
that other people, devices, and services **on the LAN** use directly — for inference, jobs, and interactive
sessions — with per-tenant API keys, GPU-seconds quotas (recurring windows), a usage ledger, and a
shape-lane scheduler. The core admission primitive (`hostagent/admission.py`) is generalized from
one-resident-tenant to **VRAM-budget-bounded co-residency of serving tenants** (constitution v1.6.0), while
training/batch jobs stay exclusive and never-preempted.

Technical approach — **reuse the surrounding platform; redesign the GPU control-plane core.** *(Revised after
the Codex architecture review, which confirmed the core is a redesign, not a light extension — `admission.py`
is single-slot and its own comments forbid holding the lock across load/unload.)* The outer layers (FastAPI
gateway, Postgres store, engine children, MLflow, Next.js console) are reused as-is. Four reuse layers plus
**one redesign**:

1. **Tenancy & auth** — per-tenant API keys resolved at the gateway; a new `tenants / api_keys / quotas /
   usage_ledger / usage_reservation` slice in `platformlib` store + migrations; **TLS required for
   multi-tenant** (FR-002a). (research R3)
2. **OpenAI-compatible + task-typed request surface** — gateway exposes `/v1/chat/completions`,
   `/v1/embeddings`, `/v1/audio/transcriptions`, and a task-typed CV endpoint, mapping to existing children;
   concurrent tenants interleave against resident children. (R2)
3. **GPU coordinator + single-authority scheduler (REDESIGN)** — replace single-slot `admission.py` with a
   coordinator **state machine** (resident set; per-model lifecycle `loading/resident/draining/evicting`;
   active-request ref-counts; generation-token **reserve → load-outside-lock → commit/rollback**;
   **drain-before-evict**) enforcing the corrected **two-bound VRAM check** (accounted set + reservations ≤
   usable budget, AND each load ≤ live free − headroom, reconciled to the real delta). **One** GPU-ordering
   authority (host agent); `gateway/app/scheduler.py` reduced to a status/routing facade; job queue
   **persisted** in Postgres; **bounded-burst / job-drain** anti-starvation. (R1, R6)
4. **Reserve-then-settle metering, atomic quotas, gated sandbox, sessions, console** — idempotent
   **reserve→settle** GPU-seconds accounting with atomic recurring-window quota (R4/R5); arbitrary jobs run in
   a hardened sandbox **gated on a feasibility spike + a new-runtime constitution amendment** (R7); sessions
   idle-cull + TTL (R9); LAN reachability (R8); console admin/observability (021).

This is a **backend + frontend + client-CLI** feature: gateway (surface, auth, quota, metering), host agent
(co-residency admission, scheduler, sandboxed jobs, sessions), `platformlib` (data model), the Next.js
console (admin + observability), and a small tenant-facing `broker` CLI.

## Technical Context

**Language/Version**: Python 3.12 (gateway, host agent, platformlib, serving children, CLI); TypeScript 5 /
React 18 on Next.js (operator console). No new language.

**Primary Dependencies**: Existing only where possible — the GPU host agent (`hostagent/` admission +
lifecycle + jobs + journal), the FastAPI gateway (`gateway/app`), `platformlib` (store + migrations), MLflow
registry (fine-tune jobs), the 010 trainer, the existing engine children (`serving/`), the Next.js console +
BFF. **New runtime components**: a hardened container sandbox runtime for jobs (gVisor `runsc` or Kata —
feasibility resolved in R7) and a tenant `broker` CLI (stdlib + `httpx`). **No vLLM/Triton** — the existing
children cover the modalities and adding heavy engines would breach Principle III (see R2).

**Storage**: Postgres relational store (018 US4) for `tenants / api_keys / quotas / usage_ledger / jobs /
sessions`, via `platformlib` migrations. MLflow registry + Garage (S3) for job artifacts/models (reused). No
new store engine.

**Testing**: `pytest` for backend (auth/quota/ledger, co-residency admission + eviction, shape-lane
scheduler, sandbox isolation, session idle-cull); contract tests for the OpenAI/jobs/admin/session APIs;
`next lint` + `next build` for the console; on-hardware quickstart drills for concurrency, co-residency,
job exclusivity, and the LAN-only boundary.

**Target Platform**: single local machine — Docker Compose infra + native WSL GPU host agent; gateway bound
to the **LAN interface** (not just localhost) reachable by LAN peers; console on `127.0.0.1:3000` via the BFF.
**P1/P3/P4 run on the current WSL host; P2 (arbitrary-tenant jobs) targets a future native-Linux GPU host**
(real `/dev/nvidia*`) required for the mandated sandbox — the spike proved WSL2 cannot host it.

**Project Type**: web platform (multi-package repo) + a new small CLI client. No new top-level language stack.

**Performance Goals**: inference adds no material per-request latency beyond auth+quota checks (target < a few
ms overhead); ≥5 concurrent tenants interleave on one resident child without dropped requests; a queued job
starts within seconds of the GPU freeing; co-residency admission decisions are O(resident set).

**Constraints**: Principle II amended (v1.6.0) — resident serving tenants' combined VRAM ≤ live free VRAM;
jobs exclusive + never preempted; admission stays a single race-free critical section. LAN-only (no public
route). Idle control-plane footprint ≤ ~3 GB (Principle III) — models load on demand; sandbox runtime is
job-time only. Requirement IDs FR-001..026, SC-001..012.

**Scale/Scope**: a handful of LAN tenants (people + a few services/devices); one GPU; a small model zoo
(bounded by disk, Principle III). Five phased slices (P1..P5).

## Constitution Check

*GATE: evaluated against constitution **v1.6.1** — PASS for P1/P3/P4; P2 (arbitrary jobs) is GATED on a
new-runtime amendment + sandbox spike (see below).*

- **I. Local-First, Single-Machine**: PASS — everything runs on the one machine; tenants are LAN peers (like
  the console already is). Explicitly **LAN-only, never public** (FR-001). No cloud dependency; models
  resolved from the local zoo/registry. No Kubernetes/multi-node.
- **II. Single-GPU, On-Demand Serving (NON-NEGOTIABLE, v1.6.1)**: PASS — the coordinator enforces the
  **corrected two-bound VRAM rule** (accounted set + reservations ≤ usable budget, AND each load ≤ live free −
  headroom, reconciled to the real delta — no double-count) through a single race-free critical section that
  **never holds the lock across load/unload** (reserve→load-outside-lock→commit); jobs take the whole GPU and
  are never preempted (FR-010, FR-023a, FR-025). Eviction drains before unload; idle-first/LRU, serving only.
- **Development Workflow (runtime allowance)**: ⚠ **P2 GATED (spike done — WSL2 infeasible)** — the sandbox
  spike **failed on WSL2** (paravirtualized GPU; no `/dev/nvidia*`; no PCI GPU for VFIO — see
  [spikes/sandbox-feasibility.md](./spikes/sandbox-feasibility.md)). **Decision: P2 arbitrary-tenant jobs run
  on a native-Linux GPU host**, gated on (1) that host migration + a passing re-run of the spike and (2) a
  new-runtime constitution amendment (gVisor/Kata is a new runtime even on Linux). P1/P3/P4 proceed on WSL.
- **III. Lightweight Footprint**: PASS with a **watch item** — the broker control plane (auth/quota/ledger/
  scheduler) is lightweight and stays within the idle budget; models load on demand. The **job sandbox
  runtime** (gVisor/Kata) adds weight but only while a job runs, not to idle infra. Co-residency raises *active*
  VRAM/RAM use but is bounded by the live-VRAM admission itself. Recorded in Complexity Tracking.
- **IV. Full Lifecycle Coverage**: PASS — adds a self-service access layer over existing serving + training;
  fine-tune jobs still flow through the registry with lineage/eval-gates (FR-011). No stage dropped.
- **V. Open-Source & Swappable**: PASS — reuses existing OSS engines behind the host agent; the OpenAI-
  compatible surface is an interface adapter, and the sandbox runtime sits behind the jobs interface (swappable
  gVisor↔Kata). No lock-in.
- **VI. Reproducibility & Observability**: PASS — every GPU consumption is ledgered (GPU-seconds) and refused
  if it can't be recorded (FR-016); metrics exposed; fine-tune runs tracked in MLflow.
- **VII. Incremental, Phase-Gated Delivery**: PASS — ships P1→P5, each verifiable on hardware before the next.

## Project Structure

### Documentation (this feature)

```text
specs/024-lan-gpu-broker/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions R1..R9
├── data-model.md        # Phase 1 — tenants/keys/quotas/ledger/jobs/sessions + admission state
├── quickstart.md        # Phase 1 — on-hardware validation drills per story
├── user-guide.md        # Tenant + owner guide (already drafted; endpoints finalized by contracts)
├── contracts/           # Phase 1 — API + internal contracts
│   ├── inference-openai.md
│   ├── jobs-api.md
│   ├── admin-api.md
│   ├── sessions-api.md
│   └── admission-scheduler.md
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
gateway/app/                     # FastAPI gateway — the LAN front door
├── tenancy/                     #   NEW: API-key auth middleware, tenant resolution
├── quota/                       #   NEW: recurring-window quota check + refusal
├── metering/                    #   NEW: per-request GPU-seconds capture → ledger
└── routes/                      #   NEW: /v1/chat, /v1/embeddings, /v1/audio/transcriptions, /v1/vision/*, /jobs, /sessions, /admin

hostagent/                       # GPU host agent — sole GPU authority
├── coordinator.py               #   REDESIGN of admission.py: resident-set state machine, reserve→load-outside-lock→commit, drain-before-evict, two-bound VRAM check
├── scheduler.py                 #   NEW: shape-lane queue (inference vs jobs FIFO) + drain mode + owner override; SOLE GPU-ordering authority (audit/retire gateway/app/scheduler.py's ordering)
├── jobs.py                      #   EXTEND: run jobs in a hardened sandbox runtime (gVisor/Kata)
├── sessions.py                  #   NEW: interactive session leases with idle-cull + TTL
├── metrics.py / journal.py      #   EXTEND: emit GPU-seconds per request/job for the ledger
└── adapters/                    #   reused engine children adapters (llm/vision/asr/embed)

platformlib/
├── store.py / storeimpl/        #   EXTEND: tenants, api_keys, quotas, usage_ledger, jobs, sessions
└── migrations/                  #   NEW migration(s) for the above tables

ui/ (Next.js console, 021)       # EXTEND: tenant admin + quota + queue/usage observability surfaces
clients/broker-cli/              # NEW: small tenant-facing `broker` CLI (submit/queue/logs/usage/session)
infra/                           # EXTEND: LAN binding (mirrored-net or portproxy), sandbox runtime config
tests/{contract,integration,unit}/  # EXTEND: new suites per contract + story
```

**Structure Decision**: Extend the existing multi-package platform in place (gateway + hostagent +
platformlib + ui) and add one new client package (`clients/broker-cli`). No new top-level service or language.
The broker is not a new daemon — it is new surface + new admission/scheduler behavior on the existing gateway
and host agent.

## Complexity Tracking

| Violation / Watch item | Why needed | Simpler alternative rejected because |
|------------------------|------------|--------------------------------------|
| **Admission → coordinator REDESIGN** (not an extension) | `admission.py` is single-slot and forbids holding the lock across load/unload (ABBA lesson); co-residency needs a resident-set state machine with ref-counts, reservations, drain, rollback | Extending the single-slot lease was the original plan; the Codex review + `admission.py:97` show it would reintroduce the documented deadlock and corrupt in-flight requests on eviction. A focused state-machine rewrite is the minimum correct design. |
| **Job sandbox = NEW RUNTIME; WSL2 infeasible → native-Linux host** (P2 gate) | FR-026 mandates isolated-kernel/VM isolation for arbitrary tenant code | Rootless-namespaces-only does NOT satisfy FR-026. Spike (2026-07-19) proved gVisor/Kata GPU isolation is infeasible on WSL2 (paravirtualized GPU; no `/dev/nvidia*`). Decision: P2 runs on a native-Linux GPU host, gated on that migration + a passing re-run + a new-runtime amendment. P1/P3/P4 unaffected on WSL. |
| Active VRAM/RAM rises with co-residency (Principle III pressure) | Concurrent multimodal serving is the feature's point (v1.6.1) | Bounded by the two-check admission itself — accounted set ≤ usable budget; idle control plane stays light; no always-resident extra engine (vLLM/Triton) added. |

## Post-Design Constitution Re-Check

*Re-evaluated after the Codex architecture review against constitution **v1.6.1** — PASS for P1/P3/P4;
P2 gated.*

- **II (NON-NEGOTIABLE)**: The revised admission-scheduler contract encodes the **corrected two-bound VRAM
  rule** (accounted ≤ usable budget AND load ≤ live-free − headroom, reconciled) and a **reserve →
  load-outside-lock → commit** protocol so the lock is never held across lifecycle I/O; serving set empty
  during a job; jobs never preempted — all assert-tested. ✅
- **Development Workflow (runtime)**: ⚠ **P2 GATED** — the job sandbox is a new runtime needing an amendment
  + a WSL2 feasibility spike before arbitrary-job execution ships. P1/P3/P4 unaffected.
- **III**: no always-resident engine added; control plane lightweight; sandbox is job-time only. ✅
- **I / IV / V / VI / VII**: unchanged — LAN-only single machine, full lifecycle, swappable OSS, everything
  ledgered (now reserve→settle), phased. ✅

## Revised delivery phasing (per Codex review)

- **P1** — one resident model; API keys + **TLS**; OpenAI adapter; **atomic** quota + reserve→settle ledger. (No co-residency, no jobs.)
- **P2** — durable, persisted **exclusive-job queue** + arbitrary-tenant job execution on a **native-Linux GPU host** — BLOCKED until the host migration + a passing sandbox re-run + a new-runtime amendment land (spike already proved WSL2 can't host it).
- **P3** — the **GPU coordinator redesign** + bounded serving co-residency.
- **P4** — additional modalities (ASR/CV) + console controls.
- **P5** — interactive sessions, only after deciding their admission class (exclusive vs sandboxed-job vs another amendment).

Two prerequisites remain before `/speckit-tasks` can encode P2/P5 fully: the **sandbox feasibility spike**
and its **runtime amendment**. P1/P3/P4 tasks can be generated now.
