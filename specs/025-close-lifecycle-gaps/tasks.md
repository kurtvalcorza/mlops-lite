---

description: "Task list for feature 025 — close lifecycle gaps"
---

# Tasks: Close Lifecycle Gaps

**Input**: Design documents from `specs/025-close-lifecycle-gaps/`

**Prerequisites**: plan.md, spec.md

**Numbering**: Continues after 024 — FR-348+, SC-175+, T594+.

**Tests**: INCLUDED — each new capability adds tests (web-free where the logic is web-free); GPU-touching legs carry an explicit on-hardware validation task (constitution gate zero).

**Organization**: Grouped by user story. US1/US2 are the committed core; US3–US6 are independently-shippable slices that may phase into follow-on increments (026+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no incomplete-task dependency)
- **[Story]**: US1 batch · US2 tabular · US3 dataset-download · US4 stream-capture · US5 hpo-progress · US6 shadow-ui
- **[HW]**: requires the target GPU hardware to validate

---

## Phase 1: Setup

- [ ] **T594** Establish the green baseline: run `make lint test spec-check` and record the current pass state as the regression reference (SC-183).

---

## Phase 2: Foundational

- [ ] **T595** Confirm no change in this feature needs a schema migration; if one emerges, create it as a NEW numbered `platformlib/migrations/*.sql` + contract update (FR-359) before dependent tasks.

---

## Phase 3: User Story 1 — Batch inference correctness (Priority: P1) 🎯 core

**Goal**: batch scores the requested version under the lease (or refuses); every admitted batch modality has a real path.

**Independent Test**: a batch for a non-resident version scores that version (offline ordering + injected predict_fn); an ASR batch completes or is rejected at submission.

- [ ] **T596** [P] [US1] Write `tests/test_batch_version_assert.py` — offline: a batch requesting version A while B is "resident" asserts/loads A before scoring (injected predict_fn + fake admission), never scores B; refuses cleanly if a job holds the GPU (FR-348/FR-350).
- [ ] **T597** [US1] In `training/flows/batch_infer.py`, load/assert the requested `model`/`registry_version` under admission before scoring (once per batch, not per record); refuse without preempting a running job (FR-348/FR-350; closes the explicit-`registry_version`-honoring gap — NOT 015's SC-068, which kept batch-vs-`@serving` scoring correct).
- [ ] **T598** [US1] Resolve the ASR batch inconsistency: either add a real ASR batch path in `batch_infer.py` OR remove `asr` from `GPU_BATCH_MODALITIES` in `hostagent/jobs.py` so it is rejected at submission — no admitted modality raises at runtime (FR-349/SC-176).
- [ ] **T599** [HW] [US1] On the RTX 5070 Ti box: validate the load-under-lease leg — a batch for a non-resident version scores it correctly while preserving one-GPU-tenant (SC-175).

**Checkpoint**: batch is correct for every admitted modality; ships as its own PR.

---

## Phase 4: User Story 2 — Tabular full modality (Priority: P2) 🎯 core

**Goal**: tabular can train→gate→serve→monitor→retrain like vision — CPU/off-lease, no heavy dep.

**Independent Test**: a tabular dataset fine-tunes → registers-with-metric → gates on a committed AUC fixture → promotes → serves; quality window scorable where labels exist.

- [ ] **T600** [P] [US2] Add `benchmarks/tabular/auc_smoke.jsonl` — a committed held-out tabular eval fixture.
- [ ] **T601** [P] [US2] Write `tests/test_tabular_eval.py` — web-free: the AUC scorer + gate over the fixture (AUC promoted from stub to committed metric) (FR-352).
- [ ] **T602** [US2] Add `training/scoring/tabular.py` — AUC scorer over the held-out fixture (pure-Python metric); wire into `training/scoring/__init__` score-at-registration (FR-352).
- [ ] **T603** [US2] Add `training/flows/tabular_finetune.py` — CPU LightGBM fine-tune mirroring `vision_finetune.py`: train → register version with tabular task/engine tags + logged metric → failure cleanup (no partial version) (FR-351/FR-354); register in `flow_dispatch`.
- [ ] **T604** [US2] Promote tabular AUC from stub to a committed metric in `gateway/app/evaluation.py` (METRICS + live serving predictor path) (FR-352).
- [ ] **T605** [US2] Wire tabular into quality monitoring (`gateway/app/quality.py`) where a per-request label exists so it can drive breach→retrain; if excluded, document the rationale in the module + `current-architecture.md` (FR-353).
- [ ] **T606** [P] [US2] Write `tests/test_tabular_finetune.py` — seam-level (dispatch, register-with-metric, failure cleanup); full CPU run live-gated.
- [ ] **T607** [US2] Confirm no heavy dependency entered the gateway/agent images and tabular holds no GPU lease (FR-354/FR-360).

**Checkpoint**: tabular is a full lifecycle modality; ships as its own PR.

---

## Phase 5: User Story 3 — Dataset byte-download in the console (Priority: P3)

- [ ] **T608** [US3] Add a BFF-proxied/presigned dataset-byte download path (`gateway/app/routers/datasets.py` + `ui/app/data`) so an operator downloads bytes without object-store creds reaching the browser (FR-355/SC-179).
- [ ] **T609** [P] [US3] Test the download path (credential never in the browser payload; correct bytes/manifest).

**Checkpoint**: data stage is fully operable from the console.

---

## Phase 6: User Story 4 — Streamed-prediction logging (Priority: P3)

- [ ] **T610** [US4] Capture predictions served over `/infer/stream` (`gateway/app/routers/stream.py`) via the existing fail-open capture seam, off the response path, identifiable by prediction id — matching the non-streamed contract (FR-356/SC-180).
- [ ] **T611** [P] [US4] Write `tests/test_stream_capture.py` — web-free seam: a streamed completion yields the same log/capture rows as non-streamed; never blocks/alters the stream.

**Checkpoint**: streamed predictions can be labeled and enter quality/shadow.

---

## Phase 7: User Story 5 — Live HPO progress (Priority: P4)

- [ ] **T612** [US5] Surface live per-trial HPO progress (completed trials + objective values) in `ui/app/training` via an in-process progress stream — no external Optuna dashboard service (FR-357/SC-181).
- [ ] **T613** [P] [US5] Test the progress surface (trials appear/update; dependency-light).

**Checkpoint**: operators can watch a study run.

---

## Phase 8: User Story 6 — Shadow-replay console UI (Priority: P4)

- [ ] **T614** [US6] Add a console surface (`ui/app/models`) to dispatch shadow-replay and read its advisory verdict via the existing `POST /models/{name}/shadow-replay` + verdict endpoints; mark verdicts clearly advisory/never-gating (FR-358/SC-182).
- [ ] **T615** [P] [US6] Test the shadow-replay UI surface (dispatch calls the existing endpoint; verdict rendered as advisory).

**Checkpoint**: the shadow-replay backend is now operator-reachable.

---

## Phase 9: Polish & Cross-Cutting

- [ ] **T616** [P] Update `docs/current-architecture.md` if any Snapshot row changed (e.g. tabular now a full modality) (FR-361).
- [ ] **T617** Run `make lint test spec-check` green; confirm no heavy dep added and the single gated promotion choke-point is intact (FR-360/SC-183).

---

## Dependencies & Execution Order

- **Setup → Foundational** first. **US1** and **US2** are the committed core and independent of each other and of US3–US6.
- Within **US2**: fixture + scorer (T600–T602) before the flow (T603) before eval/quality wiring (T604–T605).
- **US3–US6** each depend only on their existing backend; ship independently and may phase into 026+.
- **Polish** after the shipped stories.

### Parallel opportunities

- T600/T601 (fixture + eval test) parallel; T596 parallel with US2 setup; US3–US6 are mutually independent.

## Implementation Strategy

Ship the **committed core first**: US1 (smallest, highest-value correctness) → US2 (modality completion). Then take US3–US6 as independent slices in priority order, each its own PR; if any proves larger than a slice, spin it into its own feature (026+) rather than bloating this one. GPU-touching legs (T599) are validated on the target box before their SC is marked closed.

## Notes

- Behavior change is expected here (unlike 024); each change is explicit and, where it touches persisted state or external contracts, gated by FR-359.
- Never weaken an existing test to make a change pass; add tests for every new capability (SC-183).
- On-hardware SCs cannot be closed from the offline environment — mark `[HW]` tasks done only after validation on the RTX 5070 Ti.
