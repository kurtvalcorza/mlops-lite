---

description: "Task list for feature 024 — deepen modules & testability seams"
---

# Tasks: Codebase Architecture Hardening — Deepen Modules & Testability Seams

**Input**: Design documents from `specs/024-deepen-modules-seams/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/preservation.md, quickstart.md

**Tests**: INCLUDED — the Definition of Done (spec §Success Criteria SC-001..SC-003) requires a web-free
unit test for every extracted seam plus the existing live ordering leg, so test tasks are first-class here.

**Organization**: Grouped by user story so each ships as an independent, individually-revertable PR
(P1 → P2 → P3). Behavior preservation is the contract; no test is weakened to make a refactor pass.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 (store), US2 (go-live), US3 (agent), US4 (ADRs)

---

## Phase 1: Setup (Shared)

- [ ] T001 Establish the green baseline: run `make test` and record that the offline suite passes **without** `fastapi`/`httpx` installed (the parity reference for SC-001/SC-002).
- [ ] T002 [P] Create the ADR home `docs/adr/` with a short `docs/adr/README.md` (format: Context / Decision / Consequences; status values Accepted | Rejected).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: guards that must exist before any extraction, so every later slice is provably behavior-preserving.

- [ ] T003 Confirm `tests/test_store_facade.py` pins the **complete** current `store.<symbol>` surface; extend it to cover any symbol not yet asserted (this is the safety net US1 leans on).
- [ ] T004 Confirm `scripts/check_specs.py` passes for `specs/024-deepen-modules-seams/` (spec+plan+tasks present) so the `specs` CI gate is green before implementation lands.

**Checkpoint**: baseline green + facade fully pinned → extractions can begin.

---

## Phase 3: User Story 1 — Store decomposition (Priority: P1) 🎯 MVP

**Goal**: one repository module per relational aggregate under `platformlib/storeimpl/`, the S3 side split
into `platformlib/objectstore.py`, and `platformlib/store.py` reduced to a thin re-export facade — with
every call site and driver-laziness unchanged.

**Independent Test**: `pytest tests/test_store_facade.py tests/test_store_decomposition.py` passes and
`python -c "import platformlib.store, platformlib.objectstore"` succeeds with neither boto3 nor psycopg installed.

### Tests for User Story 1

- [ ] T005 [P] [US1] Write `tests/test_store_decomposition.py` — web-free per-aggregate repository tests (predictions insert+window join, write-once `labels`→`LabelExists`, capture insert/list, jobs upsert/get, policies CRUD, suggestions create/resolve/get), using fakes/temp seams in the house `tests/_activation.py` style; assert fail-open WRITE / fail-loud READ postures (FR-015).
- [ ] T006 [P] [US1] Add an import-laziness assertion: importing `platformlib.objectstore` triggers no psycopg import and importing the relational path triggers no boto3 import (FR-004).

### Implementation for User Story 1

- [ ] T007 [US1] Create `platformlib/objectstore.py` — move `s3_client()` + paginated listing helpers out of `platformlib/store.py`; boto3 imported lazily.
- [ ] T008 [P] [US1] Create `platformlib/storeimpl/predictions.py` — move the predictions rows + predictions⋈labels window join (keep it one indexed join, no O(N) object scan).
- [ ] T009 [P] [US1] Create `platformlib/storeimpl/labels.py` — move the write-once `labels` insert (PK-enforced `LabelExists`).
- [ ] T010 [P] [US1] Create `platformlib/storeimpl/capture.py` — move the capture-index rows.
- [ ] T011 [P] [US1] Create `platformlib/storeimpl/jobs.py` — move the jobs-state access.
- [ ] T012 [P] [US1] Create `platformlib/storeimpl/policies.py` — move the policy rows/status access.
- [ ] T013 [P] [US1] Create `platformlib/storeimpl/suggestions.py` — move the promotion-suggestions access.
- [ ] T014 [US1] Reduce `platformlib/store.py` to a facade: re-export every moved symbol (+ `objectstore`) so all ~28 `from platformlib import store` call sites resolve unchanged; keep psycopg/boto3 lazy (depends on T007–T013).
- [ ] T015 [US1] Run the full offline suite + `test_store_facade.py`; confirm zero call-site edits and green (SC-001/SC-004).

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

- [ ] T016 [P] [US2] Write `tests/test_promotion_ordering.py` — web-free tests over fake `registry`/`activation`: REFUSED before `registry.promote` is called (FR-265); CONFLICT before alias moves; PROMOTED captures prior pointer before overwrite then activates; each outcome maps to the status/metric in data-model.md; assert `go_live()` is referenced by exactly one caller (the operator route).

### Implementation for User Story 2

- [ ] T017 [US2] Create `gateway/app/promotion.py` (no `fastapi`/`httpx` import): `GoLiveOutcome` enum + `GoLiveResult` + `go_live(name, version, *, override, preempt, registry, activation)` encoding the ordering invariants.
- [ ] T018 [US2] Refactor `gateway/app/routers/models.py:promote` to call `promotion.go_live(...)` and map `GoLiveResult` → HTTP status + `REGISTRY_OPS` label; response contract unchanged (depends on T017).
- [ ] T019 [US2] Verify `gateway/app/scheduler.py` (`_default_promote`) and `routers/policies.py` still call `registry.promote` directly and cannot reach `go_live()` — the single-live-switch invariant (FR-008/FR-275/307/313, SC-006).
- [ ] T020 [US2] Run offline suite + `test_promotion_gate.py`/`test_promotion_modes.py` unchanged (SC-001); run `test_promote_ordering.py` on `make up` (SC-003).

**Checkpoint**: promote handler is translate→call→map only; US2 ships as its own PR.

---

## Phase 5: User Story 4 — Decision records / ADRs (Priority: P2)

**Goal**: each accepted decision and each rejected alternative is a discoverable ADR.

**Independent Test**: `ls docs/adr/` shows an ADR per decision; the rejected-path ADRs state their rationale.

- [ ] T021 [P] [US4] `docs/adr/0001-store-decomposition.md` (Accepted) — per-aggregate repositories behind the test-pinned facade; alternatives rejected.
- [ ] T022 [P] [US4] `docs/adr/0002-go-live-paths-not-merged.md` (**Rejected** alternative) — record why unifying the three promote callers was rejected (endangers the single live-switch invariant, FR-275/307/313).
- [ ] T023 [P] [US4] `docs/adr/0003-agent-stays-framework-free.md` (Accepted) — stdlib route table over introducing a web framework in the agent.
- [ ] T024 [P] [US4] `docs/adr/0004-behavior-preserving-test-parity-gate.md` (Accepted) — refactors gated by unchanged offline suite + web-free seam tests + live leg.

**Checkpoint**: ADRs land alongside the code they document (SC-008).

---

## Phase 6: User Story 3 — Agent dispatcher route-table (Priority: P3)

**Goal**: `hostagent/main.py` dispatch is an ordered stdlib route table; handlers are unit-callable; public
surface byte-preserved; agent stays pip-dep-free.

**Independent Test**: `pytest tests/test_agent_routes.py` + existing `test_agent_http.py`/`test_agent_auth.py`
pass; agent imports with zero third-party packages.

### Tests for User Story 3

- [ ] T025 [P] [US3] Write `tests/test_agent_routes.py` — each handler is invoked directly with fake `admission`/`journal`/`manager`/`jobs` (no HTTP server, no raw-path parsing); assert the public route set from contracts/preservation.md §C3.

### Implementation for User Story 3

- [ ] T026 [US3] Introduce an ordered route table (matcher → handler) in `hostagent/main.py` and split each `if path == …` branch body into a named handler function; matching keys on the parsed path.
- [ ] T027 [US3] Route `handle_get`/`handle_post` through the table (first match wins; unmatched → 404); preserve open GET probes, keyed `/health`, secret-gated `/control/*`, and byte-compatible legacy aliases (FR-012).
- [ ] T028 [US3] Confirm the agent imports/starts with no pip dependency (FR-011) and run `test_agent_http.py`/`test_agent_jobs_http.py`/`test_agent_auth.py` unchanged (SC-001/SC-007).

**Checkpoint**: agent handlers independently testable; US3 ships as its own PR.

---

## Phase 7: Polish & Cross-Cutting

- [ ] T029 [P] Review `docs/current-architecture.md` for Snapshot drift; update in-increment only if a row changed (FR-017 — none expected, since topology/authority/trust boundaries are untouched).
- [ ] T030 Run the full `quickstart.md` recipe end-to-end (offline suite → each seam test → live leg) and confirm `make lint test spec-check` green.
- [ ] T031 Confirm no new dependency entered `gateway/requirements.txt` or the agent (FR-014) and that exactly one gated promotion choke-point remains (SC-006).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (P1)** → **Foundational (P2)** blocks all stories.
- **US1 (P1)**, **US2 (P2)**, **US4 (P2)**, **US3 (P3)** each depend only on Foundational; they are otherwise independent and each is its own PR.
- **Polish (P7)** after the stories being shipped are complete.

### Within each story

- Write the seam test first (it should be red before the extraction), then implement, then run the parity suite.
- US1: object-store + per-aggregate modules (T007–T013, parallel) before the facade reduction (T014).
- US2: `promotion.py` (T017) before the router thinning (T018) before the invariant check (T019).

### Parallel opportunities

- T008–T013 (per-aggregate moves) are parallel — distinct files.
- T021–T024 (ADRs) are parallel — distinct files.
- The four stories can proceed in parallel once Foundational is done, but ship in priority order.

---

## Implementation Strategy

### MVP first (US1 only)

1. Phase 1 Setup → Phase 2 Foundational (facade fully pinned).
2. Phase 3 US1 store decomposition → validate independently → ship PR.

### Incremental delivery

US1 (store) → US2 (go-live) + US4 (ADRs for the rejected-merge decision) → US3 (agent) — each a separate,
individually-revertable PR gated by its web-free test and the preservation contracts.

---

## Notes

- [P] = different files, no dependency on an incomplete task.
- Behavior preservation is the gate: never delete or weaken an existing test to make a refactor pass (SC-001).
- Any external-contract/schema change is an explicit deviation gated by FR-016 (new numbered migration + contract update) and re-checks the plan's Constitution Check.
- Commit after each task or logical group; each user story ends at a shippable checkpoint.
