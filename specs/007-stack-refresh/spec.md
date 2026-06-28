# Feature Specification: Stack Refresh & MLflow 3.x Upgrade

**Feature Branch**: `007-stack-refresh`

**Created**: 2026-06-28

**Status**: Draft

**Input**: Deferred from 006 (the inference-tracing increment left a platform-wide MLflow 2.18→3.x
upgrade open) plus a stack-wide version audit (2026-06-28). The platform pins MLflow `2.18.0` while the
ecosystem is at `3.14.0`; several gateway/UI deps trail current; and four infra images float on
`:latest` (a reproducibility hole). 007 is a coordinated refresh that modernizes the stack **without
changing any platform behavior** — same lifecycle, same UI, same APIs.

> **Scope note**: 007 is a **dependency/version refresh**. It adds **no new lifecycle stage, no UI
> surface, no new service, and no new runtime** (MLflow stays the registry/tracking backend; Python and
> Node stay the same runtimes, only patch/minor/base-image versions move). Requirement IDs continue the
> shared space (FR-055+, SC-036+, tasks T118+). **No constitution amendment** — MLflow is an existing
> dependency (Principle V/VI), pinning floating images *strengthens* Principle VI, and a Python
> base-image bump is the same runtime family. See plan.md → Constitution Check.

> **Hard boundary (NON-NEGOTIABLE)**: the hard-won **Blackwell sm_120 GPU stack is frozen** —
> `torch==2.11.0+cu128`, `torchvision==0.26.0+cu128`, and the fine-tune libraries
> (`transformers`/`peft`/`accelerate`/`datasets`) are **NOT upgraded** in 007. They are validated against
> the GPU and the LoRA→GGUF pipeline; churning them is out of scope and explicitly a Non-Goal.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — MLflow 2.18 → 3.x across the platform (Priority: P1)

The MLflow server and **every** skinny client (gateway, training) move to `3.14.x` together, the
existing registry/run/trace history survives the backend migration, and all MLflow-backed flows behave
identically: model registry (alias-based promotion), dataset registry metadata, training run logging,
drift reports, and 006 inference traces.

**Why this priority**: MLflow is the platform's tracking + registry backbone (Principle VI) and the
single biggest version gap. Moving it unblocks the nicer 3.x GenAI tracing (a direct win for 006) and
keeps the stack on a supported line. It is also the highest-risk change, so it leads.

**Independent Test**: After the server + clients move to 3.x and the backend is migrated, the full
001–006 suite passes: register/promote a model (alias resolves), register datasets, run a LoRA
fine-tune that logs to MLflow, run a drift check, and emit inference traces visible in the MLflow UI —
all against the migrated server, with no data loss.

**Acceptance Scenarios**:

1. **Given** an existing 2.18 backend (Postgres + MinIO artifacts) with registered models, runs, and
   traces, **When** the server is upgraded to 3.x and the backend store is migrated, **Then** the
   pre-existing models/runs/traces are still listed and resolvable (no history loss).
2. **Given** the 3.x server, **When** the gateway/training skinny clients (also 3.x) call register /
   promote / log-run / log-trace, **Then** every flow behaves exactly as on 2.18 (alias promotion,
   dataset manifests, run params/metrics, traces).
3. **Given** 006 inference tracing, **When** ported to the **non-deprecated** 3.x tracing API
   (`mlflow.start_span_no_context(start_time_ns=…)` — replacing the 2.18 `MlflowClient.start_trace` /
   `end_trace` v2 path, which 3.x deprecates), **Then** `/infer` and `/infer/stream` still emit one
   correctly-timed trace each (REST with prompt/output/registry_version; stream with frame count),
   fire-and-forget + fail-open behavior unchanged.

---

### User Story 2 — Pin the floating infra images (Priority: P1)

Every Compose-published image is pinned to an explicit, reproducible version instead of `:latest`, so a
rebuild on any machine pulls the **same** MinIO / Prometheus / Grafana / mc as the validated platform.

**Why this priority**: Reproducibility is a constitution principle (VI), and `minio:latest`,
`minio/mc:latest`, `prom/prometheus:latest`, `grafana/grafana:latest` currently float — a rebuild can
silently pull a new major and break the stack. Highest value-per-risk in the refresh; pairs with US1
since both touch infra.

**Independent Test**: `docker-compose.yml` references no `:latest` tag for a platform service; a clean
`up_all` pulls the pinned versions and the foundation smoke (`test_foundation`/`test_exposure`) passes.

**Acceptance Scenarios**:

1. **Given** the compose file, **When** it is grepped for `:latest`, **Then** no platform service image
   uses it (Postgres, MinIO, mc, MLflow base, gateway base, Prometheus, Grafana all pinned).
2. **Given** the pinned images, **When** the stack is brought up clean, **Then** every service is
   healthy and the 001–006 suite passes (the pins are the validated versions).

---

### User Story 3 — Gateway Python dependency refresh (Priority: P2)

The gateway's pure-Python deps move to current minors and the Python base image moves 3.11 → 3.12,
with no behavior change to any endpoint.

**Why this priority**: Keeps the gateway on supported, patched libraries (FastAPI, uvicorn, pydantic,
boto3, prometheus-client) and a current Python. Low risk (no API surface change expected), but lower
value than US1/US2, so P2.

**Independent Test**: The gateway image builds on `python:3.12-slim` with bumped deps; every gateway
integration test (auth, serving, registry, datasets, monitor, vision, stream, tracing) passes
unchanged; `/metrics` and OpenAPI are unchanged.

**Acceptance Scenarios**:

1. **Given** bumped `gateway/requirements.txt` + `python:3.12-slim`, **When** the gateway is rebuilt,
   **Then** it starts and all protected/unprotected routes behave exactly as before.
2. **Given** the bumped pydantic/FastAPI, **When** request/response models are exercised, **Then**
   validation and serialization are unchanged (no schema drift in OpenAPI).

---

### User Story 4 — UI dependency refresh, staying on Next 15 (Priority: P3)

The operator console moves to the latest **Next 15.x** patch + current React 19 patch + tooling bumps
(TypeScript, Tailwind, PostCSS, autoprefixer, `@types/*`) — **not** Next 16 (deferred; major App-Router
risk).

**Why this priority**: Keeps the UI patched (security + tooling) with minimal risk by staying on the
validated Next 15 line. Lowest urgency.

**Independent Test**: `npm ci` + `next build` succeed on the bumped tree; the six tabs render and the
BFF (allowlist, origin guard, `[::1]`, non-leaky errors, key never in payloads) behaves identically;
`test_ui_security` / `test_ui_smoke` / `test_ui_resilience` pass.

**Acceptance Scenarios**:

1. **Given** the bumped `package.json` + refreshed `package-lock.json`, **When** the UI is rebuilt and
   the supervisor bounces the `ui` daemon, **Then** all six surfaces + the BFF behave unchanged.
2. **Given** the refresh, **When** the security test runs, **Then** the API key is still absent from all
   browser-visible payloads and the localhost-only posture holds.

---

### User Story 5 — Safe native (non-GPU) bumps (Priority: P3)

The native non-GPU libraries that are safe to move — BentoML (vision serving), Pillow, Prefect (ephemeral
flow structure) — go to current minors. The **GPU/FT stack stays frozen** (torch, torchvision,
transformers, peft, accelerate, datasets).

**Why this priority**: Small, contained patch hygiene for the CPU-side native deps, with the GPU stack
explicitly walled off. Lowest priority; deferrable.

**Independent Test**: With the bumped BentoML/Pillow/Prefect, the vision classify (`test_bento`) and the
fine-tune + drift loop (`test_finetune`, `test_drift_loop`) still pass on the frozen torch stack.

**Acceptance Scenarios**:

1. **Given** bumped BentoML/Pillow, **When** `/vision/classify` runs, **Then** it returns the same top-5
   shape on CPU.
2. **Given** the frozen GPU pins, **When** a LoRA fine-tune runs, **Then** it completes on the GPU
   exactly as validated (no torch/transformers movement).

---

### Edge Cases

- **MLflow backend reset**: a fresh `pgdata` volume (grilled) gives the 3.x server a clean store — no
  schema migration to fail, and it sidesteps the known Postgres-password-rotation-on-existing-volume
  gotcha. The trade is losing MLflow run/trace history (accepted); the re-seed (serving model + vision)
  restores registry resolution, and datasets persist on MinIO (FR-055).
- **Deprecated trace API**: 3.x keeps `MlflowClient.start_trace`/`end_trace` but deprecates the v2
  trace-start path; 007 must move 006 to `start_span_no_context` so the gateway isn't built on a
  deprecated shim (FR-057).
- **Skinny/server version skew**: all MLflow clients and the server MUST be the **same** 3.x version —
  a 2.18 client against a 3.x server (or vice-versa) is unsupported (FR-055).
- **Frozen GPU stack**: any transitive pull that would move torch/transformers/peft is a violation —
  the bumps in US3/US5 must not perturb the cu128 torch family (FR-060).
- **Pinned-image drift**: pins must be the *validated* versions, captured after a clean bring-up — not
  guessed (FR-056).
- **No regression**: the full 001–006 suite passes; every endpoint, the six UI tabs, the SSE framing,
  and the one-model-in-VRAM mutex behave identically (SC-041).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-055**: The MLflow server (`infra/mlflow`) and **all** skinny clients (`gateway/requirements.txt`,
  `training/requirements.txt`) MUST move to the same `mlflow[-skinny]==3.14.0` version. The Postgres
  backend store is **reset to a fresh volume** (grilled decision — no in-place `mlflow db upgrade`),
  trading the MLflow run/trace history (accepted loss) for zero migration risk. **Datasets are unaffected**
  (content-addressed on MinIO, not Postgres). The platform MUST be **re-seeded** so it resolves again:
  re-register + promote the serving LLM (`qwen2.5-7b` → `@serving`) and run `scripts/seed_vision_model.py`.
- **FR-056**: No platform-service image in `docker-compose.yml` MUST use `:latest`. MinIO, `minio/mc`,
  Prometheus, Grafana (and the Postgres minor + the Python base images) MUST be pinned to explicit,
  validated versions. Pins SHOULD be captured after a clean, healthy bring-up.
- **FR-057**: 006 inference tracing MUST be ported to the non-deprecated 3.x tracing API
  (`mlflow.start_span_no_context(start_time_ns=…)` + the span/trace finalize call), preserving the
  fire-and-forget, fail-open, span-outside-the-GPU-lock, and frame-count behavior exactly. The toggles
  (`MLFLOW_TRACING_ENABLED` / `MLFLOW_TRACE_CAPTURE_IO`) and the container env passthrough are unchanged.
- **FR-058**: The gateway MUST build on `python:3.12-slim` with refreshed pure-Python deps (FastAPI,
  uvicorn, pydantic, boto3, prometheus-client) at current minors; every endpoint's behavior, OpenAPI
  contract, and `/metrics` output MUST be unchanged.
- **FR-059**: The UI MUST move to the latest Next **15.x** patch + current React 19 patch + tooling
  bumps, with `package-lock.json` refreshed and committed; the six tabs and the BFF security contract
  MUST be unchanged. Next 16 is explicitly out of scope (Non-Goal).
- **FR-060**: The Blackwell GPU stack — `torch==2.11.0+cu128`, `torchvision==0.26.0+cu128`,
  `transformers`, `peft`, `accelerate`, `datasets` — MUST NOT be upgraded; the US3/US5 bumps MUST NOT
  perturb the cu128 torch family. Safe native bumps are limited to BentoML, Pillow, and Prefect.
- **FR-061**: Each tier MUST be validated against the full 001–006 integration suite (the converted
  pytest suite + UI tests + tracing tests) on the target machine before the next tier; nothing may
  regress any lifecycle, UI, tracing, or VRAM-mutex behavior.

### Key Entities *(include if feature involves data)*

- **PinnedImage**: a Compose service image at an explicit, validated tag/digest (replaces a `:latest`).
- **MlflowVersion**: the single `3.x` version shared by the server + every skinny client; the migrated
  backend store carries the existing registry/run/trace history.
- **FrozenGpuStack**: the locked cu128 torch family + FT libraries — the upgrade boundary 007 must not
  cross.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-036**: The MLflow server + all skinny clients run the same `3.x` version; pre-existing
  registry/runs/traces survive the migration and every MLflow-backed flow (registry/datasets/training/
  drift/tracing) passes unchanged.
- **SC-037**: `docker-compose.yml` contains no `:latest` for a platform service; a clean `up_all` pulls
  the pins and comes up healthy.
- **SC-038**: 006 tracing runs on the 3.x `start_span_no_context` API with REST + stream traces intact
  (attributes, frame count, fail-open, span-outside-lock) — no deprecated trace path in use.
- **SC-039**: The gateway builds on Python 3.12 with bumped deps and every endpoint + OpenAPI + metrics
  are unchanged.
- **SC-040**: The UI builds on the latest Next 15.x + React 19 patch; six tabs + BFF security contract
  unchanged.
- **SC-041**: No regression — the full 001–006 suite passes with the frozen GPU stack intact; the
  one-model-in-VRAM mutex, SSE framing, and every status code are unchanged.

## Assumptions

- **MLflow 3.x is drop-in for our usage** — we use the tracking + alias-based registry + tracing APIs,
  all of which exist in 3.x; the registry "stages" removed in 3.x are already unused (we promote via
  aliases since 004). We reset to a **fresh backend** (no schema migration) and re-seed the serving/vision
  registry pointers; the only code change is the 006 tracing port to `start_span_no_context` (FR-057).
- **The GPU stack is the constraint, not the goal** — torch/transformers were hard-won on Blackwell
  sm_120; 007 freezes them deliberately. A future increment may revisit them with their own GPU
  re-validation.
- **Single local operator, unchanged posture** — 007 changes versions only; loopback binding,
  fail-closed auth, the BFF contract, and the hybrid-GPU model all stand.
- **Pins are captured, not guessed** — the validated versions are read off a clean, healthy bring-up
  before being written into the compose file / lock files.

## Non-Goals

- **Upgrading the Blackwell GPU / fine-tune stack** (torch, torchvision, transformers, peft, accelerate,
  datasets) — frozen in 007 (FR-060); a separate increment if ever.
- **Next.js 16** — deferred; 007 stays on the validated Next 15 line (FR-059).
- **New MLflow 3.x features** (logged-models GenAI eval, prompt registry, etc.) — 007 is a version move
  + the minimal 006 tracing port, not a feature expansion. A later increment may adopt 3.x eval/tracing
  features on top of the migrated server.
- **Switching any backing service** (Postgres/MinIO/Prometheus/Grafana) — only pinned, not replaced.
