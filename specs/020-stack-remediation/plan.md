# Implementation Plan: 020 Stack Remediation — object-store exit, Bento-ectomy, agent-runtime decision

**Branch**: `claude/mlops-lite-architecture-6a7iw2` | **Date**: 2026-07-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/020-stack-remediation/spec.md`

## Summary

Retire the two stack components the 2026-07 tech-stack review flagged and settle the third with
evidence: (1) replace the archived-upstream MinIO with **Garage** (fallback **SeaweedFS**) behind
the existing boto3/`MLFLOW_S3_ENDPOINT_URL`/`platformlib.store` seam — spike-gated, migrated by an
idempotent boto3 script with per-bucket parity reports, cut over by env flip, decommissioned only
after operator confirmation; (2) replace the three BentoML children (vision/embed/tabular) with
slim FastAPI single-route children, byte-compat gated by golden request/response sets, and drop
`bentoml` from the host venv; (3) decide the agent's HTTP runtime (stdlib vs uvicorn-ASGI) with an
on-hardware streaming/multipart drill against runbook baselines — uvicorn is pre-approved and
lands only if a baseline misses, behind a dual-runtime switch so the drill can compare both.
Cross-cutting: pin GPU-budget portability (config-only bring-up on a different-VRAM machine).

## Technical Context

**Language/Version**: Python 3.11+ (host venv) / 3.12 (gateway container). No new runtime.

**Primary Dependencies**: Existing FastAPI/uvicorn/httpx/pydantic/boto3/mlflow-skinny stack.
**New in the HOST venv:** `fastapi` + `uvicorn` (libraries — the slim children (US2) and the
optional agent ASGI runtime (US3) share one pin; the host venv previously had neither).
**Removed:** `bentoml` (+ exclusive transitive deps) from the host venv; the `minio/minio` and
`minio/mc` images from compose after decommission. **Infra swap:** `dxflrs/garage` (pinned digest)
replaces the MinIO service 1:1; `infra/garage/` carries its config + one-shot bootstrap (bucket +
key creation — the `createbuckets` replacement). No broker, no scheduler, no ORM.

**Storage**: S3-compatible object store (Garage; identical bucket/prefix layout: `datasets`,
`models`, `results`, `mlflow`) + Postgres 17 (unchanged). Migration tool: `scripts/migrate_store.py`
on boto3 (already a dependency) — bidirectional, idempotent, per-bucket parity report.

**Testing**: pytest offline suite (FakeS3 seams unchanged — they fake the *client*, so they are
store-agnostic by construction); live golden flows per quickstart; per-child golden
request/response byte-parity sets; on-hardware runtime drill (`scripts/agent_stream_drill.py`).

**Target Platform**: single WSL2 machine, hybrid topology (Compose control plane + native host
agent). GPU budget parameterized (`VRAM_GB`), 12 GB today, portable to other sizes (US4).

**Project Type**: infra swap + native-service slimming inside the existing two-package layout —
no new top-level packages.

**Performance Goals**: no regression — golden flows and suite pass unchanged (SC-128); streaming
TTFT/stall baselines per runbook (SC-132); migration of the current object population completes
within one operator session.

**Constraints**: Principle II untouched (no admission-semantics changes anywhere in scope);
Principle III — replacement store idle RSS ≤ MinIO's (SC-130), host-venv package count strictly
decreases net of the fastapi/uvicorn adds (SC-131); constrained drive — both stores co-resident
only for the migration window, headroom checked first.

**Scale/Scope**: 4 buckets, single-node store (replication factor 1); 3 child services; 1 HTTP
surface; ~10–14 tasks expected. Requirement IDs FR-198..207, SC-127..133, tasks from T401.

## Constitution Check

*GATE: evaluated against constitution v1.4.1 — PASS (one wording-refresh flag, no violations).*

- **I. Local-First**: PASS — Garage is a single local container in the same compose stack;
  nothing leaves the machine. Migration is local disk-to-disk.
- **II. Single-GPU lease (NON-NEGOTIABLE)**: PASS — nothing in 020 touches admission, lifecycle,
  or swap semantics. The US3 runtime swap is transport-only: the framework-free forward functions
  (`forward_engine`/`forward_engine_multipart`) are reused verbatim by the ASGI app, and the agent
  contract tests + swap/lifecycle suites must pass unchanged on both runtimes. The US2 children
  are below the adapter boundary (the agent still owns spawn/admission).
- **III. Lightweight Footprint**: PASS — Garage's single-node idle RSS is measured in the spike
  and gates cutover (SC-130: ≤ incumbent). `fastapi`/`uvicorn` enter the host venv as libraries
  (no new resident service); `bentoml` leaves it. Net resident processes: unchanged (store
  swapped 1:1, children swapped 1:1, agent unchanged).
- **IV. Full Lifecycle**: PASS — no stage added or dropped; every stage's storage moves intact.
- **V. Open-Source & Swappable**: PASS — this increment *is* Principle V exercised twice
  ("Replacements are allowed; lock-in is not"). ⚠ **Wording flag (operator-gated, mirrors 017's
  T342 / 018's T378)**: the principle's illustrative "default stack" sentence still names MinIO,
  DVC, Ollama + BentoML, and Evidently — after 020 it will be stale on two more counts. A
  constitution wording refresh (rule text unchanged, default-stack list updated to the real
  stack) is queued as a polish task alongside the still-open T378.
- **VI. Reproducibility & Observability**: PASS — MLflow tracking unchanged; the migration
  report and runtime baseline record are durable artifacts; store/children keep health + metrics.
- **VII. Phase-Gated**: PASS — three independently shippable stories, each with its own gate
  (spike → migrate → cutover → decommission; per-child golden gates; measure → verdict → maybe
  upgrade) and its own rollback.

*Post-design re-check (after Phase 1)*: PASS — no design artifact introduced a new resident
service, a new data shape, or an admission-path change.

## Project Structure

### Documentation (this feature)

```text
specs/020-stack-remediation/
├── spec.md              # /speckit-specify output (done)
├── plan.md              # This file
├── research.md          # Phase 0 (R1–R8)
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1 — per-US validation drills incl. the [HW] runtime drill
├── contracts/
│   ├── store-migration.md   # migrate_store.py CLI + parity-report shape + cutover env contract
│   └── children-api.md      # byte-compat route contract per child + golden-set format
└── tasks.md             # /speckit-tasks output (NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
infra/garage/            # NEW: garage.toml (single-node, replication=1) + init.sh (one-shot
│                        #   bucket+key bootstrap — the createbuckets replacement)
docker-compose.yml       # garage service (pinned digest) added behind env-selected endpoint;
│                        #   minio + createbuckets removed at decommission (last task)
scripts/migrate_store.py # NEW: boto3 mirror, bidirectional, idempotent, parity report (JSON)
scripts/agent_stream_drill.py  # NEW: US3 drill — TTFT/stalls/multipart/disconnect/swap-contention
serving/children/        # NEW: vision_service.py, embed_service.py, tabular_service.py —
│                        #   slim FastAPI single-route children (+ /readyz), same routes/ports
serving/bento/           # DELETED at US2 completion (service.py, embed_service.py,
│                        #   tabular_service.py, run scripts, bentoml requirements)
hostagent/adapters/      # launch-command edits ONLY (_bento_cpu.py → _child_cpu.py rename,
│                        #   vision.py run-script path) — adapter contract untouched
hostagent/main.py        # US3 (conditional): AGENT_RUNTIME=stdlib|uvicorn switch; ASGI app
│                        #   reusing the existing framework-free forward functions
training/requirements.txt / serving/children/requirements.txt  # venv: -bentoml, +fastapi+uvicorn
platformlib/topology.py  # store endpoint resolution unchanged (env-driven); no code change
tests/                   # goldens/ fixtures + migrate-store unit tests (two-FakeS3 mirror),
                         #   children contract tests, dual-runtime agent HTTP tests
```

**Structure Decision**: no new top-level packages. The children move from `serving/bento/` to
`serving/children/` (framework-neutral name — the directory outlives any one serving library);
`infra/garage/` mirrors the existing `infra/prometheus`/`infra/mlflow` pattern. All store access
continues through the existing seams — zero call-site changes expected outside compose/env.

## Complexity Tracking

> No Constitution Check violations. Open items: (a) the Principle V default-stack **wording
> refresh** is operator-gated and queued with T378 — 020 does not edit the constitution; (b)
> `fastapi`+`uvicorn` are new host-venv *libraries* (not services), justified under Principle III
> in research R5/R7 — they replace a strictly heavier framework (`bentoml` depends on both
> uvicorn and starlette transitively, so the net dependency count drops).
