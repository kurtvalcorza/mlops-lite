---

description: "Task list for feature 024 — deepen modules & testability seams"
---

# Tasks: Codebase Architecture Hardening — Deepen Modules & Testability Seams

**Input**: Design documents from `specs/024-deepen-modules-seams/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/preservation.md, quickstart.md

**Numbering**: Continues the global sequence after 023 — FR-329+, SC-165+, T558+.

**Tests**: INCLUDED — the Definition of Done (spec §Success Criteria SC-165..SC-167) requires a web-free
unit test for every extracted seam plus the existing live ordering leg, so test tasks are first-class here.

**Organization**: Grouped by user story so each ships as an independent, individually-revertable PR
(P1 → P2 → P3). Behavior preservation is the contract; no test is weakened to make a refactor pass.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 (store), US2 (go-live), US3 (agent), US4 (ADRs), US5 (behavior-preserving gap closures)

---

## Phase 1: Setup (Shared)

- [ ] **T558** Establish the green baseline: run `make test` and record the current pass state as the parity reference for SC-165. (Correction: the offline suite installs `-r gateway/requirements.txt`, so `fastapi`/`httpx` ARE present — do NOT try to run it without them; SC-166 is a per-seam import-isolation test, not a fastapi-free suite run.)
- [ ] **T559** [P] Create the ADR home `docs/adr/` with a short `docs/adr/README.md` (format: Context / Decision / Consequences; status values Accepted | Rejected).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: guards that must exist before any extraction, so every later slice is provably behavior-preserving.

- [ ] **T560** Confirm `tests/test_store_facade.py` pins the **complete** current `store.<symbol>` surface; extend it to cover any symbol not yet asserted (this is the safety net US1 leans on).
- [ ] **T561** Confirm `scripts/check_specs.py` passes for `specs/024-deepen-modules-seams/` (spec+plan+tasks present) so the `specs` CI gate is green before implementation lands.

**Checkpoint**: baseline green + facade fully pinned → extractions can begin.

---

## Phase 3: User Story 1 — Store decomposition (Priority: P1) 🎯 MVP

**Goal**: one repository module per relational aggregate under `platformlib/storeimpl/`, the S3 side
consolidated into the existing `platformlib/s3io.py`, and `platformlib/store.py` reduced to a thin re-export
facade — with every call site and driver-laziness unchanged.

**Independent Test**: `pytest tests/test_store_facade.py tests/test_store_decomposition.py` passes and
`python -c "import platformlib.store, platformlib.s3io"` succeeds with neither boto3 nor psycopg installed.

### Tests for User Story 1

- [ ] **T562** [P] [US1] Write `tests/test_store_decomposition.py` — web-free per-aggregate repository tests (predictions insert+window join, write-once `labels`→`LabelExists`, capture insert/list, jobs upsert/get, policies CRUD, suggestions create/resolve/get), using fakes/temp seams in the house `tests/_activation.py` style; assert the exact postures (Codex round-5): the prediction/capture insert primitives **propagate** (raise) their errors — the fail-open/drop-counter posture lives in the `quality.log_prediction`/`capture_input` WRAPPER (which catches, increments the drop counter, and invalidates the broken cached connection), NOT in the repository (a repo that swallowed would silently break both the counter and the stale-connection reset). Label attach is **fail-loud**, and window/policy/job READS are fail-loud. So: test repo error-**propagation** here, and test the fail-open/drop-counter at the quality-wrapper boundary (FR-343). **Impl note:** `tests/test_store_decomposition.py` pins the decomposition structurally (facade re-exports resolve to the exact `storeimpl/*` functions; no aggregate SQL left in the facade) + the driver-laziness; the per-aggregate SQL behavior + the propagate-vs-wrapper-fail-open posture are already exercised by the unchanged existing suite (journal/quality/jobs tests over the real primitives), so per-aggregate fake-cursor tests were not duplicated. Test-parity is the behavior gate.
- [x] **T563** [P] [US1] Add an import-laziness assertion: importing `platformlib.s3io` triggers no psycopg import and importing the relational path triggers no boto3 import (FR-332). (This requires making s3io's boto3 import lazy — see T564.)

### Implementation for User Story 1

- [x] **T564** [US1] Consolidate the object-store access into the **existing** `platformlib/s3io.py` (the shared Garage authority used by batch/quality/validation) — move `store.s3_client()` + `list_keys`/`list_common_prefixes` there, homed alongside s3io's `_s3()`, make s3io's `boto3` import **lazy** so the relational path stays boto3-free, and re-export the helpers from the `store` facade. **Do NOT create a new `objectstore.py`** — it would be a second S3 home, undercutting the single-home goal (Codex). **Impl note:** `_s3()` and `s3_client()` are NOT merged into one factory — `_s3()` builds fresh per call and that is load-bearing (`test_s3io_client_missing_creds_raises_by_name` relies on it), so behavior-preservation (024) keeps them distinct: one home, two functions.
- [x] **T565** [P] [US1] Create `platformlib/storeimpl/predictions.py` — move the predictions rows + predictions⋈labels window join (keep it one indexed join, no O(N) object scan).
- [x] **T566** [P] [US1] Create `platformlib/storeimpl/labels.py` — move the write-once `labels` insert (PK-enforced `LabelExists`).
- [x] **T567** [P] [US1] Create `platformlib/storeimpl/capture.py` — move the capture-index rows.
- [x] **T568** [P] [US1] Create `platformlib/storeimpl/jobs.py` — move the jobs-state access.
- [x] **T569** [P] [US1] Create `platformlib/storeimpl/policies.py` — move the policy rows/status access.
- [x] **T570** [P] [US1] Create `platformlib/storeimpl/suggestions.py` — move the promotion-suggestions access.
- [x] **T571** [US1] Reduce `platformlib/store.py` to a facade: re-export every moved symbol (+ the `s3io` object-store helpers) so all ~28 `from platformlib import store` call sites resolve unchanged; keep psycopg/boto3 lazy (depends on T564–T570). **Scope note (Codex):** this MUST also relocate the `serving_llm` pointer SQL (`store.py:485-510` — `set`/`get`/`clear_serving_llm`) into a storeimpl repository — it is aggregate-specific SQL still in `store.py`, so the import-only facade + SC-168 cannot be met while it remains. (US2's T577 relocates the `registry.py` *wrappers* on top of this US1 move, so sequence T577 after T571.) **Shared-plumbing home (Codex):** `store.py` also owns the *shared* relational infrastructure `dsn`, `connect`, `bootstrap`, `ensure_schema`, `SCHEMA_VERSION`, and `TABLES` (lines 128-192) — not aggregate SQL, but not re-exportable from nowhere. Give it an explicit home: relocate it into a dedicated `platformlib/storeimpl/_engine.py` (connection + bootstrap/schema plumbing; drivers stay lazy) — or extend `storeimpl/_base.py`, which the current plan leaves untouched — and re-export it from the facade so `store.connect`/`store.bootstrap`/`store.ensure_schema`/`store.dsn`/`store.SCHEMA_VERSION`/`store.TABLES` resolve unchanged (migration startup and every `store.connect`/`store.bootstrap` caller depend on this). Pin these exact symbols in `test_store_facade.py`.
- [x] **T572** [US1] Run the full offline suite + `test_store_facade.py`; confirm zero call-site edits and green (SC-165/SC-168).

**Checkpoint**: store.py is a facade with no aggregate SQL inline; US1 ships as its own PR.

---

## Phase 4: User Story 2 — Go-live ordering extraction (Priority: P2)

**Goal**: the promote ordering lives in web-free `gateway/app/promotion.py` returning a `GoLiveOutcome`
result; `routers/models.py:promote` becomes a thin outcome→HTTP/metric adapter; the use-case has exactly
one caller (the operator route).

**Independent Test**: `pytest tests/test_promotion_ordering.py` (web-free, fakes) passes; the existing
`tests/test_promote_ordering.py` live leg passes on `make up`; promote request/response + status/metric
mapping byte-identical (contracts/preservation.md §C2).

### Tests for User Story 2

- [x] **T573** [P] [US2] Write `tests/test_promotion_ordering.py` — web-free tests over fake `registry`/`activation`: REFUSED before `registry.promote` is called (FR-265); CONFLICT before alias moves (pre-check path, `conflict` only); **the post-promote TOCTOU conflict** — `activate()` raising `ActivationError` *after* promote already emitted `ok` yields `metric_statuses == ["ok", "conflict"]`, 409, alias left moved (invariant 4, a preserved existing behavior); PROMOTED captures prior pointer before overwrite then activates; each outcome maps to the status/metric in data-model.md; assert `go_live()` is referenced by exactly one caller (the operator route).

### Implementation for User Story 2

- [x] **T574** [US2] Create `gateway/app/promotion.py` (no `fastapi`/`httpx` import): `GoLiveOutcome` enum + `GoLiveResult` + `go_live(name, version, *, override, preempt, registry, activation)` encoding the ordering invariants.
- [x] **T575** [US2] Refactor `gateway/app/routers/models.py:promote` to call `promotion.go_live(...)` and map `GoLiveResult` → HTTP status + `REGISTRY_OPS` label; response contract unchanged (depends on T574).
- [x] **T576** [US2] Verify `gateway/app/scheduler.py` (`_default_promote`) and `routers/policies.py` still call `registry.promote` directly and cannot reach `go_live()` — the single-live-switch invariant (FR-336/FR-275/307/313, SC-170).
- [x] **T577** [US2] Relocate ONLY the serving-LLM **pointer CRUD** primitives (`get_serving_llm`/`set_serving_llm`/`restore_serving_llm`, `gateway/app/registry.py`) into a dedicated relational repository — they are pure Postgres state. **Keep `active_serving_llm_name` OUT of the relational repository**: it reads the pointer AND calls `llmresolve.adopt_active_llm` (MLflow) for the pointer-unset adoption + configured-default fallback, so it stays a higher-level web-free selection policy — moving it into `storeimpl` would either drag MLflow into the relational layer or drop the adoption behavior (changing which LLM serves after upgrade). Call sites and go-live capture/restore stay behavior-identical. Rationale in `docs/adr/0005-serving-llm-pointer-not-mlflow-alias.md`.
- [ ] **T578** [US2] Run offline suite + `test_promotion_gate.py`/`test_promotion_modes.py` unchanged (SC-165); run `test_promote_ordering.py` on `make up` (SC-167).

**Checkpoint**: promote handler is translate→call→map only; US2 ships as its own PR.

---

## Phase 5: User Story 4 — Decision records / ADRs (Priority: P2)

**Goal**: each accepted decision and each rejected alternative is a discoverable ADR.

**Independent Test**: `ls docs/adr/` shows an ADR per decision; the rejected-path ADRs state their rationale.

- [ ] **T579** [P] [US4] `docs/adr/0001-store-decomposition.md` (Accepted) — per-aggregate repositories behind the test-pinned facade; alternatives rejected.
- [ ] **T580** [P] [US4] `docs/adr/0002-go-live-paths-not-merged.md` (**Rejected** alternative) — record why unifying the three promote callers was rejected (endangers the single live-switch invariant, FR-275/307/313).
- [ ] **T581** [P] [US4] `docs/adr/0003-agent-stays-framework-free.md` (Accepted) — stdlib route table over introducing a web framework in the agent.
- [ ] **T582** [P] [US4] `docs/adr/0004-behavior-preserving-test-parity-gate.md` (Accepted) — refactors gated by unchanged offline suite + web-free seam tests + live leg.
- [x] **T583** [P] [US4] `docs/adr/0005-serving-llm-pointer-not-mlflow-alias.md` (Accepted) — record why the platform serving-LLM selection is a Postgres pointer, not an MLflow `@serving` alias (aliases are per-registered-model; the selection is cross-model). Delivered in this PR since it documents the pre-existing spec-022 decision, and it names the T577 relocation follow-up.

**Checkpoint**: ADRs land alongside the code they document (SC-172).

---

## Phase 6: User Story 3 — Agent dispatcher route-table (Priority: P3)

**Goal**: `hostagent/main.py` dispatch is an ordered stdlib route table; handlers are unit-callable; public
surface byte-preserved; agent stays pip-dep-free.

**Independent Test**: `pytest tests/test_agent_routes.py` + existing `test_agent_http.py`/`test_agent_auth.py`
pass; agent imports with zero third-party packages.

### Tests for User Story 3

- [x] **T584** [P] [US3] Write `tests/test_agent_routes.py` — each handler is invoked directly with fake `admission`/`journal`/`manager`/`jobs` (no HTTP server, no raw-path parsing); assert the public route set from contracts/preservation.md §C3.

### Implementation for User Story 3

- [x] **T585** [US3] Introduce an ordered route table (matcher → handler) in `hostagent/main.py` and split each `if path == …` branch body into a named handler function; matching keys on the parsed path.
- [x] **T586** [US3] Route `handle_get`/`handle_post` through the table (first match wins; unmatched → 404); preserve open GET probes, keyed `/health`, secret-gated `/control/*`, and byte-compatible legacy aliases (FR-340).
- [x] **T587** [US3] Confirm the agent imports/starts with no pip dependency (FR-339) and run `test_agent_http.py`/`test_agent_jobs_http.py`/`test_agent_auth.py` unchanged (SC-165/SC-171).

**Checkpoint**: agent handlers independently testable; US3 ships as its own PR.

---

## Phase 6b: User Story 5 — Behavior-preserving gap closures (Priority: P3)

**Goal**: give the input-drift PSI math a web-free offline test, and reconcile stale docs/comments to
shipped reality — no production behavior change.

**Independent Test**: `pytest tests/test_drift_psi.py` passes offline; no reconciled doc/comment
contradicts `specs/*/tasks.md` or the shipped code.

- [ ] **T588** [P] [US5] Write `tests/test_drift_psi.py` — web-free unit test for `gateway/app/monitoring.py:psi` (identical distributions → ~0; shifted → expected bucketed PSI; empty/degenerate inputs handled), no live stack (FR-346/SC-173).
- [ ] **T589** [P] [US5] Reconcile the README 023 on-hardware status against `specs/023-platform-architecture-hardening/tasks.md` (drills marked passing on the RTX 5070 Ti) — comment/doc only, no code change (FR-347/SC-174).
- [ ] **T590** [P] [US5] Reconcile stale comments in `gateway/app/evaluation.py`: the `:17` "shadow-replay deferred" note (shipped as feature 016) and the WER/recall@k "guidance stub" docstrings (fixtures shipped in 015) — comment-only, no code-path change (FR-347/SC-174).

**Checkpoint**: drift math is offline-tested and the ground-truth docs match reality; US5 ships as its own small PR.

---

## Phase 7: Polish & Cross-Cutting

- [ ] **T591** [P] Review `docs/current-architecture.md` for Snapshot drift; update in-increment only if a row changed (FR-345 — none expected, since topology/authority/trust boundaries are untouched).
- [ ] **T592** Run the full `quickstart.md` recipe end-to-end (offline suite → each seam test → live leg) and confirm `make lint test spec-check` green.
- [ ] **T593** Confirm no new dependency entered `gateway/requirements.txt` or the agent (FR-342) and that exactly one gated promotion choke-point remains (SC-170).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (P1)** → **Foundational (P2)** blocks all stories.
- **US1 (P1)**, **US2 (P2)**, **US4 (P2)**, **US5 (P3)**, **US3 (P3)** each depend only on Foundational; they are otherwise independent and each is its own PR.
- **Polish (P7)** after the stories being shipped are complete.

### Within each story

- Write the seam test first (it should be red before the extraction), then implement, then run the parity suite.
- US1: object-store + per-aggregate modules (T564–T570, parallel) before the facade reduction (T571).
- US2: `promotion.py` (T574) before the router thinning (T575) before the invariant check (T576).

### Parallel opportunities

- T565–T570 (per-aggregate moves) are parallel — distinct files.
- T579–T582 (ADRs) are parallel — distinct files.
- T588–T590 (US5 PSI test + doc reconciliations) are parallel — distinct files.
- The five stories can proceed in parallel once Foundational is done, but ship in priority order.

---

## Implementation Strategy

### MVP first (US1 only)

1. Phase 1 Setup → Phase 2 Foundational (facade fully pinned).
2. Phase 3 US1 store decomposition → validate independently → ship PR.

### Incremental delivery

US1 (store) → US2 (go-live) + US4 (ADRs for the rejected-merge decision) → US5 (PSI test + doc reconcile) →
US3 (agent) — each a separate, individually-revertable PR gated by its web-free test and the preservation
contracts. US5 is behavior-preserving and can land any time after Foundational; it's sequenced late only
because it's lowest-leverage, not because anything blocks it.

---

## Notes

- [P] = different files, no dependency on an incomplete task.
- Behavior preservation is the gate: never delete or weaken an existing test to make a refactor pass (SC-165).
- Any external-contract/schema change is an explicit deviation gated by FR-344 (a schema change → new numbered migration; an API change → contract update; independently) and re-checks the plan's Constitution Check.
- Commit after each task or logical group; each user story ends at a shippable checkpoint.
