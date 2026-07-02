# Implementation Plan: Platform Re-Architecture — GPU Host Agent, Durable State, Shared Contracts, Closed Loop

**Branch**: `018-platform-rearchitecture` | **Date**: 2026-07-02 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/018-platform-rearchitecture/spec.md` (clarified
2026-07-02, 4 grilled decisions encoded). Derived from the 2026-07 architecture review
([docs/architecture-review-2026-07.md](../../docs/architecture-review-2026-07.md)).

## Summary

Consolidate the four lease-tenant native daemons (llama supervisor, whisper supervisor, BentoML
vision, trainer) plus the two off-lease CPU services and the babysitter's fan-out into **one
torch-free native `gpu-host-agent`**: in-process race-free GPU admission (NVML, no lockfile
protocol at completion), every engine a supervised **child process** behind one tenant lifecycle
with thin per-engine adapters, **transactional** evict→free→load swap, a durable job journal,
and a directly-scraped metrics endpoint. All inference traffic enters via the agent's single
stable endpoint (clarified). A **shared contracts package** (`platformlib/`) is used by both the
gateway image and the native host. High-churn state (predictions, labels, capture index, job
records) moves to the already-provisioned `gateway` Postgres database, both runtimes writing
directly through the shared storage client (clarified). A gateway-resident **policy scheduler**
closes the Principle IV loop: declarative per-model policies (API + UI editor, clarified) drive
scheduled checks → modality-correct retrain on the latest dataset → gate → `manual`/`suggest`/
`auto-on-green` promotion consuming shadow verdicts. The review's P0 correctness fixes land
first, unconditionally. Migration is strangler-style and phase-gated: the agent honors the
lockfile while any external tenant remains; the suite is green at every merged phase.

## Technical Context

**Language/Version**: Python 3.12 (native host agent, in the existing `~/mlops-train` venv);
Python 3.11+ (gateway container). TypeScript/Next 15 for the two UI touches (policy editor,
promotion suggestions). No new runtime.

**Primary Dependencies**: Existing FastAPI/httpx/pydantic/boto3/mlflow-skinny stack. **New:**
`pynvml` (host only — replaces per-call `nvidia-smi` forks with in-process NVML reads) and
`psycopg[binary]` (gateway + host, US4 only — the relational storage client). The shared
`platformlib/` package is **stdlib-only** (dataclasses + validation helpers) to avoid version
skew between the two runtimes. No broker, no scheduler framework, no ORM/migration tool.

**Storage**: MinIO unchanged (datasets, model artifacts, captured payloads, reports). US4 adopts
the provisioned-but-unused `gateway` Postgres database for predictions / labels / capture index
/ job records / policies, with idempotent hand-rolled DDL bootstrap (house style — no Alembic).
Pre-US4, the agent's journal is an append-only JSONL file in the agent state dir; policies live
as objects in MinIO. Job-record write path: agent → Postgres directly (clarified).

**Testing**: `pytest` — the existing 218-test suite is the migration safety net (green at every
merged phase, FR-177); new unit tests per component (admission, lifecycle, adapters via fake
engines, transactional swap, journal replay, policy scheduler, storage client with a fake/local
store); on-hardware runbook for SC-106–111 (GPU behaviors can't run in CI, per house practice).

**Target Platform**: Win11 + WSL2 + NVIDIA (hybrid-GPU model unchanged): agent + UI native,
everything else Compose. Agent listens on **:8100** during migration (coexists with legacy
daemons); per-engine fold-in flips that engine's `*_URL` in the gateway to the agent; at
completion a single `AGENT_URL` replaces the six daemon URLs.

**Project Type**: Local MLOps platform — new top-level `hostagent/` package + new shared
`platformlib/` package; retirements in `serving/`, `training/trainer.py`, `supervisor/`.

**Performance Goals**: Cold-load / warm-inference within 10% of the 017 baseline (SC-107);
quality/shadow windows <5s at 10k predictions (SC-111); zero per-poll subprocess forks (SC-110);
swap latency unchanged (~2.5s evict+load).

**Constraints**: Principle II at every instant of every phase (including mid-migration lockfile
interop); idle RAM ≤ ~3 GB; no new resident service; frozen GPU stack untouched; external API
byte-compatible (FR-177); default behaviors unchanged (refuse-if-held, manual promotion).

**Scale/Scope**: Single operator, 5 modalities, ~10k-prediction monitoring windows. Replaces 4
supervisor implementations + trainer daemon + babysitter fan-out with 1 agent + 5 adapters;
deletes the lockfile protocol at completion.

## Constitution Check

*GATE: must pass before Phase 0. Re-checked after design.* Constitution **v1.4.1**.

| Principle | Assessment |
|---|---|
| **I. Local-First** | ✅ No cloud; consolidation is intra-host. Offline behavior unchanged. |
| **II. Single-GPU On-Demand (NON-NEGOTIABLE)** | ✅ **Preserved and strengthened.** The rule mandates "a single, race-free GPU lease: a single-slot admission mechanism with no TOCTOU window" — it does not mandate the lockfile *implementation*. In-process admission inside one owner removes the cross-process protocol surface entirely; swap becomes transactional (evict→free→load under one decision), closing the handoff-sniping window; training/HPO/batch protection becomes structural (no fail-open probe). Mid-migration the agent participates in the lockfile so the invariant holds across the boundary (FR-166, cross-boundary contention test each such phase). ⚠️ **Wording check**: v1.4.x text *describes* the lease as a file-based mechanism across daemons in places; a **v1.5.x description refresh** (not a rule change) should note the lease is realized as the host agent's in-process admission — **confirm with the operator before ratifying** (mirrors 017's T342 handling). |
| **III. Lightweight Footprint** | ✅ Resident native processes ~8 → 2; the babysitter shrinks; `pynvml` is a tiny host-only library; Postgres is **already resident** for MLflow (the `gateway` DB has been provisioned since 001 — adopting it adds no service). Idle RAM budget re-verified on hardware (SC-107). |
| **IV. Full Lifecycle Coverage** | ✅ **This increment makes the loop actually closed** (scheduled checks → modality-correct retrain → gate → suggest/auto promotion). No stage dropped. |
| **V. OSS & Swappable** | ✅ Engine adapters make "swappable serving engine" a real interface (SC-114); MLflow/MinIO/Prometheus/Grafana unchanged behind existing seams. |
| **VI. Reproducibility & Observability** | ✅ Improved: direct agent scrape removes the gateway observability SPOF; journal + audit records make jobs and auto-promotions durable; all policy checks observable. |
| **VII. Incremental, Phase-Gated** | ✅ Strangler migration, one engine per phase, suite green at each merge (FR-177/SC-113); on-hardware verification per phase per house practice. |

**Verdict: PASS** — no Complexity Tracking entries. Open item: the **v1.5.x wording refresh**
(description, not rule) — resolve with the operator in Phase 0 (mirrors 017).

## Project Structure

### Documentation (this feature)

```text
specs/018-platform-rearchitecture/
├── spec.md          # clarified spec (4 grilled decisions)
├── plan.md          # this file
├── research.md      # Phase 0 — decisions: NVML, package shape, port strategy, DDL bootstrap,
│                    #   scheduler placement, auth posture, absent-engine state, constitution wording
├── data-model.md    # Phase 1 — Tenant, EngineAdapter, JobRecord, ModelPolicy, PromotionSuggestion,
│                    #   AuditRecord, Prediction/Label/CaptureIndex rows
├── quickstart.md    # Phase 1 — per-phase validation guide (offline + on-hardware)
├── contracts/
│   ├── agent-api.md         # the agent's single endpoint: engines, infer, jobs, admission, unload
│   ├── platformlib.md       # the shared package: topology, tenant ids, typed payloads, storage client
│   ├── policy-api.md        # gateway policy CRUD + scheduler behavior + promotion modes
│   └── store-schema.md      # US4 relational schema + write-once label constraint + backfill
├── checklists/requirements.md
└── tasks.md         # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
platformlib/                     # NEW shared contracts package (stdlib-only)
├── topology.py                  # tenant ids, engine registry, agent port, holder labels
├── contracts.py                 # typed payloads: health, admission, job, policy, verdicts
└── store.py                     # storage client (S3 helpers now; Postgres client in US4)

hostagent/                       # NEW single native GPU host agent (torch-free)
├── main.py                      # HTTP surface (single stable endpoint), wiring, shutdown
├── admission.py                 # in-process single-slot admission; NVML reads w/ TTL cache;
│                                #   lockfile-interop shim (dropped at retirement)
├── lifecycle.py                 # shared tenant lifecycle: load→ready→drain→idle-release→reap
├── swap.py                      # transactional evict→free→load under the admission lock
├── jobs.py                      # train/HPO/batch/shadow execution (subprocess-per-run preserved)
├── journal.py                   # append-only JSONL journal → Postgres at US4; restart replay
├── metrics.py                   # /metrics (direct Prometheus target)
└── adapters/
    ├── llama.py                 # llama-server child (from serving/llama/supervisor.py)
    ├── whisper.py               # whisper-server child (from serving/whispercpp/supervisor.py)
    ├── vision.py                # torch vision child (from serving/bento/service.py)
    ├── embed.py                 # CPU, off-admission (from serving/bento/embed_service.py)
    └── tabular.py               # CPU, off-admission (from serving/bento/tabular_service.py)

gateway/app/
├── settings.py                  # NEW central settings (imports platformlib.topology)
├── policies.py + routers/policies.py   # US3: policy CRUD + validation + scheduler state
├── scheduler.py                 # US3: asyncio loop — checks → breach → retrain → suggest/auto
├── swap.py / routers/*.py       # groundwork fixes, then thinned to agent calls per fold-in phase
├── quality.py / shadow.py       # US4: window resolution via platformlib.store queries
└── monitoring.py / routers/monitor.py  # groundwork: reserve-before-launch; modality-aware spec

infra/postgres/init.sql          # US4: gateway DB schema bootstrap reference (DDL also applied
                                 #   idempotently by platformlib.store at startup)
infra/prometheus/prometheus.yml  # agent scrape target
supervisor/supervise.py          # shrinks to {agent, ui}
serving/ + training/trainer.py   # retired per fold-in phase (gpu_lease.py last, with its tests
                                 #   rewritten against the agent admission API)

ui/
├── app/monitor/page.tsx         # US3: policy editor (CRUD via BFF)
├── app/models/page.tsx          # US3: promotion suggestions + shadow verdict surface
└── lib/gw-allowlist.ts          # policy + suggestion routes

tests/
├── test_agent_admission.py, test_agent_lifecycle.py, test_agent_swap_txn.py,
├── test_agent_journal.py, test_agent_adapters.py (fake engine), test_lockfile_interop.py,
├── test_policy_crud.py, test_policy_scheduler.py, test_promotion_modes.py,
├── test_store_client.py, test_label_write_once.py, test_backfill.py
└── (existing 218 remain the regression net; lease-internal tests rewritten at retirement)
```

**Structure Decision**: Two new top-level packages (`platformlib/` shared, `hostagent/` native)
— named to avoid the stdlib `platform` module. The gateway image `COPY`s `platformlib/`; the
host venv imports it by path from the repo (no publishing). Legacy daemon files are deleted in
the same phase their adapter lands, never left dual-mastered.

## Complexity Tracking

> No Constitution Check violations. Open items: (a) the **v1.5.x wording refresh** (description
> of the lease mechanism — operator confirms, Phase 0); (b) `pynvml` + `psycopg` are new
> *libraries* (not services) — justified under Principle III in research.md.

## Phase 0 — Research (see research.md)

Decisions to close: NVML via `pynvml` vs continued `nvidia-smi` (chosen: pynvml + TTL cache;
smi remains the fallback path); stdlib-only contracts package vs pydantic-shared (chosen:
stdlib; rationale: version-skew risk between container and host venv); agent port strategy
during migration (:8100 + per-engine URL flips); DDL bootstrap without a migration tool
(idempotent `CREATE TABLE IF NOT EXISTS` at storage-client init, schema mirrored in
`init.sql` for fresh installs); scheduler placement (gateway lifespan task — always-on,
survives host restarts); **agent control-surface auth posture** (localhost bind + the existing
opt-in shared-secret header for unload/job control — house posture, deferred clarify item);
**absent-engine-binary state** (adapter reports `unavailable`; ASR stays opt-in; platform
health continues to exclude optional engines from `all_healthy`); the constitution wording
question (operator).

## Phase 1 — Design & Contracts

- **data-model.md**: Tenant, EngineAdapter, JobRecord (+ state machine), ModelPolicy,
  PromotionSuggestion, AuditRecord, Prediction/Label/CaptureIndex rows, journal entry format.
- **contracts/**: `agent-api.md` (single endpoint: engine listing/health, per-modality infer
  passthrough incl. SSE, job submit/status, unload/swap control, admission semantics, error
  vocabulary 400/409/502/507 preserved); `platformlib.md` (topology constants, typed payloads,
  storage client surface, import rules for both runtimes); `policy-api.md` (CRUD shapes,
  validation, scheduler tick behavior, breach→retrain→suggest/auto flows, audit records);
  `store-schema.md` (tables, uniqueness/write-once constraints, indexes for window queries,
  backfill procedure).
- **quickstart.md**: per-phase validation — offline suites per phase; on-hardware: bring-up
  process count (SC-106), latency parity (SC-107), swap contention stress (SC-108), restart
  journal drill (SC-109), gateway-down scrape check (SC-110), 10k-window timing (SC-111),
  injected-breach loop drill (SC-112).
- **Agent context update**: point `CLAUDE.md`'s managed block at this plan.

## Phase 2 — Tasks (/speckit-tasks)

Expected shape (dependency-ordered, one fold-in per phase): US1 groundwork (6 fixes, each
independently mergeable) → `platformlib/` + settings → agent skeleton (admission + lifecycle +
journal + metrics + lockfile interop) → LLM fold-in → ASR fold-in → vision fold-in →
embed/tabular fold-in → jobs fold-in (trainer retirement) → lockfile retirement + babysitter
shrink + lease-test rewrite → US3 policies (parallel track after US1: CRUD → scheduler →
retrain wiring → suggest/auto + UI) → US4 store (client → schema → cutover+backfill → window
queries) → on-hardware validation sweep. Task IDs continue at **T343+**.
