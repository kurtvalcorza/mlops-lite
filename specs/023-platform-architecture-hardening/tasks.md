---

description: "Implementation task list for post-021 architecture hardening and delivery integrity"
---

# Tasks: Platform Architecture Hardening & Delivery Integrity

**Input**: Design documents from `/specs/023-platform-architecture-hardening/`

**Prerequisites**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Numbering**: Continues after 022's T489.

**Status (2026-07-12, implementation PR)**: the OFFLINE slice is built and checked off with
evidence in `quickstart.md` §Evidence. Still open: `[HW]`/populated-store drills (T517, T528,
T536, T555, T556 — and T557, which gates on them), the external branch-protection setting half of
T509 (the workflow + jobs exist; an admin marks them required), and the US7 module extractions
(T539, T543–T545) + their completion gate T549 — independent, contract-preserving refactors
deferred to land separately without conflict churn (see the US7 commit).

**Tests**: Required. Correctness, security, migration, activation, and transport tests are written
before their implementation tasks. `[HW]` tasks require target-machine evidence and cannot be
completed from hosted CI.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: May run in parallel because it owns different files and has no unmet dependency.
- **[Story]**: Maps directly to a user story in `spec.md`.
- Every task names concrete repository paths and a validation outcome.

## Phase 1: Setup — reproducible foundations

- [x] **T490** Add the dependency-light backend developer/test environment in
  `requirements-dev.txt` and document the exact clean-checkout install command; include pytest,
  Ruff, and all dependencies needed for offline collection without torch/CUDA/model packages
  (FR-289/294).
- [x] **T491** [P] Normalize offline/live/hardware markers and skip reasons in `pyproject.toml` and
  `tests/conftest.py`; prove ordinary CI selects the complete offline suite and never silently
  deselects an import failure (FR-293, SC-155).
- [x] **T492** [P] Create the initial `.github/workflows/quality.yml` skeleton with stable, independent
  `backend`, `ui`, `compose`, and `specs` jobs, least-privilege permissions, concurrency cancellation,
  and dependency caching boundaries from `contracts/delivery-gates.md` (FR-290..296).

**Checkpoint**: A clean environment and job structure exist; story-specific gates can be filled in.

---

## Phase 2: User Story 1 — Correct internal routing (Priority: P1) 🎯 MVP slice 1

**Goal**: Remove the last executable pre-018 live-evaluation endpoints.

**Independent Test**: With only `AGENT_URL`, fake HTTP calls resolve to agent LLM/vision engine paths
and the retired-port guard passes.

### Tests

- [x] **T493** [P] [US1] Add failing standalone-load and URL-resolution tests in
  `tests/test_evaluation_topology.py` for LLM, vision, explicit overrides, and absence of retired
  defaults (FR-277..280).

### Implementation

- [x] **T494** [US1] Change `gateway/app/evaluation.py` to derive live predictor bases through
  `platformlib.topology.agent_url()` and the canonical engine paths while preserving isolated module
  loading and injected predictors (FR-277..279).
- [x] **T495** [US1] Extend `scripts/check_specs.py` or add a focused repository guard to scan
  executable Python/PowerShell/shell routing for retired ports while excluding historical docs and
  explicit negative fixtures; wire it into the specs/backend gate (FR-280).
- [x] **T496** [US1] Run `tests/test_evaluation_topology.py`, existing evaluation/gate tests, and the
  retired-port scan; record SC-152 evidence in `specs/023-platform-architecture-hardening/quickstart.md`.

**Checkpoint**: Live evaluation uses the consolidated topology independently of later hardening.

---

## Phase 3: User Story 2 — Enforce the agent trust boundary (Priority: P1) 🎯 MVP slice 2

**Goal**: Require an internal credential at the privileged execution-plane boundary.

**Independent Test**: Every protected route rejects missing/wrong keys before effects and succeeds
with the gateway-injected key; public probes remain available.

### Tests

- [x] **T497** [P] [US2] Add failing auth/config/public-route/side-effect-order tests in
  `tests/test_agent_auth.py`, covering stdlib transport, open-development warning, deprecated secret
  migration, constant-time comparison seam, and secret-redaction assertions (FR-281..288).
- [x] **T498** [P] [US2] Extend gateway client tests (`tests/test_serving_client.py`,
  `tests/test_agent_jobs_http.py`, router tests) to require `X-Agent-Key` injection and prevent
  cross-origin redirect forwarding (FR-286).

### Implementation

- [x] **T499** [US2] Implement `hostagent/auth.py` with exact public-route allow-list, secret/file
  resolution, fail-closed startup policy, `AGENT_ALLOW_OPEN`, deprecated input warning, constant-time
  comparison, and stable 401/403 payloads per `contracts/agent-security.md`.
- [x] **T500** [US2] Gate all routes in `hostagent/main.py` before body parsing/domain effects; reduce
  `/healthz` and `/readyz` to documented public shapes and keep `/metrics` secret-free (FR-281..285).
- [x] **T501** [US2] Add `AGENT_API_KEY` server-side settings and header injection to every gateway
  agent client in `gateway/app/settings.py`, `gateway/app/serving.py`, routers, scheduler/evaluation
  adapters, platform health/metrics, and shared client helpers; consolidate clients where mechanical
  to avoid missed call sites (FR-286).
- [x] **T502** [P] [US2] Update `.env.example`, `scripts/gen_secrets.ps1`, `scripts/gen_secrets.sh`,
  `hostagent/run.sh`, `scripts/up_all.ps1`, and approved host tools to generate/pass the internal key
  without command-line/log exposure; document one-release deprecated-secret migration (FR-284..288).
- [x] **T503** [US2] Run auth, serving, job, platform-health, SSE, BFF security, and secret-scan tests;
  manually prove startup refusal/open-development warning and record SC-153 evidence.

**Checkpoint**: Direct agent callers cannot bypass the gateway or consume execution resources
without the internal credential.

---

## Phase 4: User Story 3 — Reproducible mandatory gates (Priority: P1) 🎯 MVP slice 3

**Goal**: Make the existing quality posture executable on every PR and locally reproducible.

**Independent Test**: A clean hosted runner passes all four jobs without GPU, model download, live
stack, or secrets.

### Implementation and tests

- [x] **T504** [P] [US3] Complete the `backend` job in `.github/workflows/quality.yml`: Python 3.12,
  clean `requirements-dev.txt` install, Ruff, full offline pytest, test report artifact, and explicit
  live/hardware skips (FR-289/290/293/294).
- [x] **T505** [P] [US3] Complete the `ui` job and `ui/package.json` runtime/lint metadata: pinned Node,
  `npm ci`, supported lint command, production build/type-check, and no reused `node_modules`
  (FR-291).
- [x] **T506** [P] [US3] Complete the `compose` job plus non-secret `.env.ci.example`/temporary env
  generation to run `docker compose config --quiet` without start/pull (FR-292/294).
- [x] **T507** [P] [US3] Implement `scripts/check_specs.py` with artifact, link, placeholder,
  ID uniqueness/order, story/task coverage, and spec-only unchecked-task validation; complete the
  `specs` job (FR-296).
- [x] **T508** [US3] Add a `make test`, `make lint`, `make ui-check`, `make compose-check`, and
  `make spec-check` interface or cross-platform documented equivalents in `Makefile`/README, ensuring
  local commands match CI behavior (contracts/delivery-gates.md).
- [ ] **T509** [US3] Run all workflow commands from a clean checkout, open a validation PR, configure
  the stable job names as required branch checks in GitHub, and record SC-154/155 evidence (repository
  workflow plus external branch-protection setting).

**Checkpoint**: The P1 hardening MVP is complete and required on every future PR.

---

## Phase 5: User Story 4 — One migration history (Priority: P2)

**Goal**: Safely evolve existing and fresh `gateway` databases before 022 adds serving state.

**Independent Test**: Empty, recognized legacy, repeated, concurrent, checksum-drift, and
newer-schema cases satisfy `contracts/schema-migrations.md` with no fixture data loss.

### Tests

- [x] **T510** [P] [US4] Add Postgres-backed migration tests in `tests/test_migrations.py` for fresh
  apply, recognized legacy adoption, exact final shape, row preservation, no-op repeat, concurrent
  runners, transaction rollback, checksum mismatch, and newer-schema refusal (FR-297..304).

### Implementation

- [x] **T511** [US4] Implement the immutable migration discovery/ledger/checksum/advisory-lock runner
  in `platformlib/migrations.py` with explicit minimum/current compatibility APIs and bounded metrics
  hooks (FR-297..301).
- [x] **T512** [US4] Add `platformlib/migrations/001_baseline.sql` representing the existing store
  schema and implement exact legacy-shape verification/stamping; prove every table/index/constraint
  in current `platformlib/store.py` is represented (FR-297/300).
- [x] **T513** [US4] Refactor `platformlib/store.py` bootstrap to call/check migrations and remove its
  embedded full-schema DDL; reduce `infra/postgres/init.sql` to database/bootstrap ownership only
  (FR-297/302).
- [x] **T514** [US4] Make gateway startup in `gateway/app/main.py` the normal migration owner and make
  host-agent/store writers fail readiness/writes on incompatible schema without attempting evolution
  (FR-299/301).
- [x] **T515** [P] [US4] Add `scripts/migrate_db.py` status/apply commands and a safe documented
  `pg_dump`/restore helper or exact platform commands; no destructive automatic rollback (FR-303/304).
- [x] **T516** [P] [US4] Export migration version/pending/outcome/duration metrics through gateway
  monitoring code and add a failure status to platform health without DSN/SQL exposure.
- [ ] **T517** [US4] On a copy of populated target state, perform backup+restore verification, apply
  baseline adoption twice, compare data counts/constraints, and record SC-156/157 evidence `[HW/store]`.

**Checkpoint**: All future relational changes use one ordered, verifiable source of truth.

---

## Phase 6: User Story 5 — Recover LLM activation (Priority: P2; integrates with 022)

**Goal**: Make 022 promotion/go-live converge safely across MLflow, Postgres, and the agent.

**Independent Test**: Failure after every operation step plus restart/retry converges to verified
target or previous identity without duplicate reload or false prediction attribution.

### Prerequisite

022 T461–T466 and T469 (pointer, resolver, adapter binding, reload primitive, honest identity) must
exist or be implemented in the same integration sequence. 023 does not duplicate those capabilities.
**Merged since the `42f8c6e` baseline (PR #65 `1008dcc`)**: 022's single-shot recovery primitives —
`swap.TargetUnresolvable` (probe-before-evict), `registry.restore_serving_llm` + the `pointer_error`
degraded outcome, and agent-reported desired-vs-resident identity — already exist. US5 wraps them in
the durable `ActivationOperation`; extend them, do not re-derive (see contract §Prior art, research R5).

### Tests

- [x] **T518** [P] [US5] Add pure state-machine/idempotency/serialization tests in
  `tests/test_activation.py` for every state/transition and conflicting operation key (FR-305..314).
- [x] **T519** [P] [US5] Add failure-injection/restart reconciliation tests in
  `tests/test_activation_recovery.py` for failures after prepare, alias, pointer, reload acceptance,
  resident success, and rollback substeps; assert resident-based prediction identity (SC-158).

### Implementation

- [x] **T520** [US5] Add the next immutable SQL migration for `activation_operations` and any 022
  active-pointer schema in `platformlib/migrations/`; implement repository accessors/CAS transitions
  in `platformlib/store.py` or an extracted activation repository (FR-305/306/314).
- [x] **T521** [US5] Implement `gateway/app/activation.py` as the durable state machine from
  `contracts/promotion-activation.md`: validation, serialization, idempotent steps, rollback,
  sanitized evidence, and desired/resident read model (FR-305..314).
- [x] **T522** [US5] Integrate the existing gated operator promote path in `gateway/app/registry.py`
  and `gateway/app/routers/models.py` with activation submission; keep policy-driven text-generation
  live switches disabled (FR-307/313).
- [x] **T523** [US5] Extend the 022 host-agent reload command in `hostagent/main.py`, lifecycle/swap,
  and shared contracts by keying its EXISTING same-target no-op and pre-eviction probe
  (`swap.TargetUnresolvable`, already merged in #65) by `operation_id`, and ADDING the genuinely-new
  `operation_id` idempotency store, conflicting-target reject, and exact resident verification
  (FR-307/308/312). Do not re-implement the probe/no-op — wrap them.
- [x] **T524** [US5] Start bounded activation reconciliation from gateway lifespan in
  `gateway/app/main.py`; resume non-terminal/retryable degraded operations and expose exact status
  through models/serving APIs without blocking unrelated gateway startup (FR-309..311).
- [x] **T525** [P] [US5] Extend 022 UI model/serving surfaces to show desired vs resident identity,
  activation state/error, retry/rollback operator actions, and never label incomplete desired state
  as serving; update `ui/lib/gw-allowlist.ts` only for required routes.
- [x] **T526** [P] [US5] Add bounded-cardinality activation outcome/reconcile-duration metrics and a
  degraded activation rule/runbook in Prometheus configuration (FR-314/321..323).
- [x] **T527** [US5] Run the full failure matrix and existing promotion/gate/shadow/policy suite;
  demonstrate automated policy cannot live-switch LLM and record SC-158.
- [ ] **T528** [US5] [HW] Execute 100 accepted rapid switches, client-timeout idempotency, and
  job-holder refusal on the target GPU; capture agent identity + `nvidia-smi` evidence for SC-159.

**Checkpoint**: 022 may be declared correctness-complete only when activation recovery and honest
resident identity pass.

---

## Phase 7: User Story 6 — Bound and simplify agent transport (Priority: P2)

**Goal**: One stdlib transport with deterministic request, thread, timeout, and shutdown bounds.

**Independent Test**: Oversized, chunked, saturated, slow, disconnect, stream, and shutdown cases
remain within bounds and preserve domain contracts.

### Tests

- [x] **T529** [P] [US6] Add failing transport-bound tests in `tests/test_agent_limits.py` for JSON,
  multipart, chunked/unknown length, auth-before-buffer, worker/queue saturation, timeouts, and
  graceful shutdown (FR-315..320).
- [x] **T530** [P] [US6] Expand REST/SSE golden parity coverage in
  `tests/test_agent_engines_http.py`, `tests/test_agent_stream_drill.py`, and job HTTP tests before
  transport removal (FR-319, SC-161).

### Implementation

- [x] **T531** [US6] Replace raw `ThreadingHTTPServer` usage in `hostagent/main.py` with a bounded
  stdlib server/worker implementation and configurable safe defaults for workers and queue
  (FR-316/320).
- [x] **T532** [US6] Implement exact endpoint body limits and counted streaming/chunked reads before
  full buffering; return stable 413 and preserve multipart forwarding within bounds (FR-317).
- [x] **T533** [US6] Add explicit read/write/probe/shutdown timeouts and graceful accept-stop/drain/
  child-cleanup behavior while preserving journal interruption semantics (FR-318).
- [x] **T534** [P] [US6] Add bounded-cardinality request, saturation, rejection, disconnect, and
  latency metrics in `hostagent/metrics.py` (FR-321).
- [x] **T535** [US6] Run all parity/limit tests and `scripts/agent_stream_drill.py`; after parity,
  delete `hostagent/asgi.py`, the `AGENT_RUNTIME=uvicorn` branch, uvicorn-only runtime docs/deps, and
  duplicate-only tests while retaining shared behavior tests (FR-315).
- [ ] **T536** [US6] [HW] Repeat REST/stream/disconnect/saturation drills on the target host, measure
  peak threads/memory, verify no admission leak, and record SC-160/161 evidence.

**Checkpoint**: The execution plane has one authenticated, bounded, tested transport.

---

## Phase 8: User Story 7 — Actionable operations and current architecture (Priority: P3)

**Goal**: Expose failures, reduce internal change coupling, and maintain a trustworthy current-state
entry point without changing process topology.

**Independent Test**: Synthetic metrics fire every rule, characterization tests prove behavioral
parity across extractions, and the documentation checklist finds no current-state conflict.

### Tests and characterization

- [x] **T537** [P] [US7] Add metrics contract tests in `tests/test_metrics_contract.py` for required
  operation families and forbidden high-cardinality labels (FR-321).
- [x] **T538** [P] [US7] Add Prometheus rule syntax/synthetic-evaluation tests in
  `tests/test_alert_rules.py` for every required alert and runbook link (FR-322/323).
- [ ] **T539** [P] [US7] Add characterization tests around `platformlib/store.py`,
  `gateway/app/evaluation.py`, `gateway/app/scheduler.py`, `hostagent/main.py`, and the large training
  UI page before extraction; record public payload/import/side-effect contracts (FR-324/325).

### Implementation

- [x] **T540** [P] [US7] Add fixed-cardinality request/admission/swap/reload/job/scheduler/database/
  object-store outcome and latency metrics in owning gateway/agent modules, reusing a small helper
  only where names/labels are identical (FR-321).
- [x] **T541** [P] [US7] Add `infra/prometheus/rules/mlops-lite.yml`, load it from
  `infra/prometheus/prometheus.yml`/Compose, and cover wedged engine, prolonged holder, repeated
  scheduler/activation/migration failures, low disk, and unavailable stores (FR-322/323).
- [x] **T542** [P] [US7] Add alert-focused Grafana panels/links and operator remediation sections in
  `monitoring/README.md`; do not add Alertmanager or external credentials (FR-323).
- [ ] **T543** [US7] Extract relational repositories/migration concerns from `platformlib/store.py`
  behind the existing public facade, keeping callers and stored contracts compatible (FR-324/325).
- [ ] **T544** [US7] Extract evaluation live predictors/benchmark metrics and scheduler external
  adapters/state transitions into coherent internal modules under `gateway/app/`, preserving
  standalone load and injected test seams (FR-324/325).
- [ ] **T545** [US7] Separate host-agent route dispatch from retained stdlib transport and split
  `ui/app/training/page.tsx` orchestration into tested hooks/panels, without a new process or route
  contract (FR-324/325).
- [x] **T546** [P] [US7] Refresh `README.md` to actual merged/spec status and concise current topology;
  link `docs/architecture-review-2026-07-11.md`, label the prior review historical, and resolve
  present-tense `in progress`/retired-framework contradictions (FR-326/327).
- [x] **T547** [P] [US7] Add a reusable current-architecture checklist under `.specify` or
  `docs/` covering topology, data authority, trust boundaries, runtime status, ports, and commands;
  reference it from future spec/checklist guidance (FR-328).
- [x] **T548** [US7] Process the observed Principle VI DVC wording conflict as a separate
  constitution v1.5.2 wording-only amendment in `.specify/memory/constitution.md`, explicitly
  preserving the rule while naming the implemented content-addressed dataset registry.
- [ ] **T549** [US7] Run synthetic alert evaluation, all characterization/regression tests, resource
  comparison, and documentation checklist; record SC-162..164.

**Checkpoint**: Failures are locally actionable and current architecture is trustworthy without
new services or behavior drift.

---

## Phase 9: Polish, security review, and complete validation

- [x] **T550** [P] Run `scripts/check_specs.py`, link/placeholder/ID checks, `git diff --check`, and
  update all 023 artifacts only for implementation-discovered facts; preserve historical evidence.
- [x] **T551** [P] Run Ruff and the complete offline pytest suite from a clean `requirements-dev.txt`
  environment; attach job/test evidence and resolve warnings introduced by 023.
- [x] **T552** [P] Run clean `npm ci`, supported lint, and production build/type-check; validate BFF
  allow-list and secret non-exposure for activation additions.
- [x] **T553** [P] Run Compose config and Prometheus/Grafana provisioning/rule validation with
  non-secret CI values.
- [x] **T554** Perform a focused security review of agent bind/auth/redirect/header/body-limit/logging
  behavior and a repository secret scan; verify all unauthorized paths are side-effect free.
- [ ] **T555** Perform migration backup/restore/adoption/concurrency evidence on a populated database
  copy and verify gateway/agent compatibility failure modes.
- [ ] **T556** [HW] Run the complete 023 target-hardware sequence: auth gateway flow, LLM/vision eval,
  activation rapid-switch/job refusal, retained transport stream/saturation, metrics/alerts, and
  resource budget; attach timestamps, commit, hardware profile, and observed one-tenant invariant.
- [ ] **T557** Update `README.md`, `docs/architecture-review-2026-07-11.md`, quickstart evidence, and
  increment status to implementation-complete only after T550–T556 pass.

## Dependencies & Execution Order

### Phase dependencies

- Setup T490–T492 starts first.
- US1, US2, and US3 are independent P1 slices after relevant setup; recommended order is US1 → US2
  → US3 so the defect and trust boundary are protected by the new gates.
- US4 depends on the backend CI environment and must precede any 022/US5 relational schema addition.
- US5 depends on US4 and 022 foundations T461–T466/T469.
- US6 depends on US2 so authentication is tested before body buffering and transport removal.
- US7 metrics/alerts can begin after metric contracts; module extractions wait for characterization
  tests and relevant US1/US4/US6 changes to avoid repeated conflicts.
- Polish waits for the desired stories and all required `[HW]` gates.

### Parallel opportunities

- T490–T492 can proceed independently.
- US1 tests and US2 tests can be authored in parallel.
- Backend/UI/Compose/spec CI jobs T504–T507 are separate files/surfaces.
- US4 CLI/metrics work can proceed after runner interfaces stabilize.
- US5 UI/metrics can proceed after the activation read contract stabilizes.
- US7 metrics, alerts, docs, and separate characterization suites can proceed in parallel.

## Implementation Strategy

### Hardening MVP

1. T490–T492 setup.
2. US1 routing repair.
3. US2 agent authentication.
4. US3 mandatory CI.
5. Stop and validate: the current platform is more correct, secure, and enforceably tested without
   waiting for 022 or broader refactoring.

### 022 readiness

1. Land US4 migration history.
2. Implement required 022 resolver/identity foundations.
3. Land US5 activation operation/reconciliation.
4. Complete 022 UI/hardware behavior only after recovery evidence passes.

### Operational completion

1. Bound/remove duplicate transport (US6).
2. Add metrics/alerts and characterization-backed internal extraction (US7).
3. Complete security, migration, CI, and target-hardware gates.
