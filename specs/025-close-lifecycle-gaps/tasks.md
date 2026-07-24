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

- [ ] **T596** [P] [US1] Write `tests/test_batch_version_assert.py` — offline: a batch requesting version A while B is "resident" asserts/loads A before scoring (injected predict_fn + fake admission), never scores B; refuses cleanly if a job holds the GPU (FR-348/FR-350). MUST also assert the **restore/unload** of the temporary target after the batch — on BOTH a successful batch and a batch that raises mid-scoring — so the prior desired target is left resident (Codex — see T597).
- [ ] **T597** [US1] In `training/flows/batch_infer.py`, load/assert the requested `model`/`registry_version` under admission before scoring (once per batch, not per record); refuse without preempting a running job (FR-348/FR-350; closes the explicit-`registry_version`-honoring gap — NOT 015's SC-068, which kept batch-vs-`@serving` scoring correct). **Scope note (Codex):** this CANNOT be done in `batch_infer.py` alone — the engine endpoints accept no requested model/version and the only targeted reload (serving-LLM pointer) is refused once `_gpu_batch_active`. The substantive work is an **agent-side target-version load/assert seam** (host agent + engine wiring) called before scoring; `batch_infer.py` only drives it. **Restore requirement (Codex):** the batch drives the SAME resident engine online `/infer` uses (`batch_infer.py` docstring §Lease discipline), so loading version A for the batch leaves A serving online traffic until the next reload/idle-release. The load/assert MUST be paired with a `finally` that **restores the prior desired/resident target (or unloads the temporary one)** on both success and exception — never leave the batch's temporary version serving online requests.
- [ ] **T598** [US1] ASR batch (**corrected, Codex**): ASR is ALREADY rejected at submission — `hostagent/jobs.py` validates against `BATCH_MODALITIES` (which excludes `asr`); `GPU_BATCH_MODALITIES` (which lists `asr`) is only serving-holder protection, consulted *after* validation. So there is **no accepted-then-failed bug** to fix. This task is therefore net-new-only: **if** batched ASR is wanted, add `asr` to `BATCH_MODALITIES` + a real ASR path in `batch_infer.py`; **otherwise drop this task** — no action needed (FR-349/SC-176 revised accordingly, or descoped).
- [ ] **T599** [HW] [US1] On the RTX 5070 Ti box: validate the load-under-lease leg — a batch for a non-resident version scores it correctly while preserving one-GPU-tenant (SC-175).

**Checkpoint**: batch is correct for every admitted modality; ships as its own PR.

---

## Phase 4: User Story 2 — Tabular full modality (Priority: P2) 🎯 core

**Goal**: tabular can train→gate→serve→monitor→retrain like vision — CPU/off-lease, no heavy dep.

**Independent Test**: a tabular dataset fine-tunes → registers-with-metric → gates on a committed AUC fixture → promotes → serves; quality window scorable where labels exist.

- [ ] **T600** [P] [US2] Add `benchmarks/tabular/auc_smoke.jsonl` — a committed held-out tabular eval fixture.
- [ ] **T601** [P] [US2] Write `tests/test_tabular_eval.py` — web-free: the tabular **prediction factory** + the existing pure-Python `auc` metric + gate over the fixture (AUC promoted from stub to committed metric) (FR-352).
- [ ] **T602** [US2] Add `training/scoring/tabular.py` as the tabular **prediction factory** — a `predict_fn(rows, modality, version)` returning probability scores, the collaborator `training/scoring/__init__.score_and_log` requires (mirroring the vision/embeddings scorers). **Do NOT re-implement AUC** — the pure-Python `auc` metric already exists at `gateway/app/evaluation.py:153-173` and is in `METRICS`; T604 only promotes it from stub (FR-352).
- [ ] **T603** [US2] Add `training/flows/tabular_finetune.py` — CPU LightGBM fine-tune mirroring `vision_finetune.py`: train → register version with tabular task/engine tags + logged metric → failure cleanup (no partial version) (FR-351/FR-354); register in `flow_dispatch`. **Scope note (Codex):** registering in `flow_dispatch` is NOT sufficient — `platformlib.topology.TRAINABLE_MODALITIES` excludes tabular and the `finetune` job kind is `gpu=True`, so admission work is needed to admit tabular training as **CPU/off-lease** (no GPU acquisition, FR-354). ALSO the tabular child serves the single configured `TABULAR_MODEL` name via `@serving`, so a version trained under any other output name won't be served — constrain/validate the output name to the served name (or add dynamic tabular serving-selection + reload) before claiming train→promote→serve parity.
- [ ] **T604** [US2] Promote tabular AUC from stub to a committed metric in `gateway/app/evaluation.py` (METRICS + live serving predictor path) (FR-352).
- [ ] **T605** [US2] Wire tabular into quality monitoring (`gateway/app/quality.py`) where a per-request label exists so it can drive breach→retrain; if excluded, document the rationale in the module + `current-architecture.md` (FR-353). **Scope note (Codex):** editing `quality.py` alone is insufficient — the tabular `/predict` router never calls `quality.log_prediction`, returns no per-row prediction IDs, and the tabular child reports no registry version, so there are no version-scoped prediction rows for `quality.window()`. This task MUST also add the tabular serving/router **prediction-logging + identity contract** (per-row prediction IDs + the actually-served version) first. Each row MUST log the **numeric probability `score`** (not the response dict, not the thresholded class) — `quality.score_window` feeds stored values straight to `evaluation.auc`, which ranks numeric scores (a dict breaks sorting; the binary class discards the ranking AUC measures). **Scheduler-retrain gap (Codex):** to actually *drive* breach→retrain, tabular MUST also be added to `gateway/app/scheduler.py`'s `MODALITY_TASK` map (line 50-51). Today `scheduler.py:412` does `MODALITY_TASK[policy["modality"]]` (a direct index, not `.get`), so a tabular policy tick raises `KeyError`, which the tick swallows as `check_error` — a breach is never detected and the promised retrain never launches. Add the `tabular` mapping AND a tabular policy-tick test that asserts a breach is detected (not swallowed as a check error).
- [ ] **T606** [P] [US2] Write `tests/test_tabular_finetune.py` — seam-level (dispatch, register-with-metric, failure cleanup); full CPU run live-gated.
- [ ] **T607** [US2] Confirm no heavy dependency entered the gateway/agent images and tabular holds no GPU lease (FR-354/FR-360).

**Checkpoint**: tabular is a full lifecycle modality; ships as its own PR.

---

## Phase 5: User Story 3 — Dataset byte-download in the console (Priority: P3)

- [ ] **T608** [US3] Add a gateway/BFF **byte-proxy** dataset download path (`gateway/app/routers/datasets.py` + `ui/app/data`) so an operator downloads bytes without object-store creds reaching the browser (FR-355/SC-179). **Scope note (Codex):** (a) it MUST be a byte **proxy**, NOT a bare presigned URL — the presigned URL is generated from the internal S3 endpoint (`garage:3900`, `datasets.py:130-134`) the browser cannot resolve (exactly why the existing `download_url` is unusable). (b) MUST also add the exact download route to `ui/lib/gw-allowlist.ts` — the BFF rejects any unmatched route before injecting the key (current dataset entries allow only the manifest GET), so without the allowlist entry the browser gets a BFF 404 even when the gateway endpoint works.
- [ ] **T609** [P] [US3] Test the download path (credential never in the browser payload; correct bytes/manifest).

**Checkpoint**: data stage is fully operable from the console.

---

## Phase 6: User Story 4 — Streamed-prediction logging (Priority: P3)

- [ ] **T610** [US4] Capture predictions served over `/infer/stream` (`gateway/app/routers/stream.py`) via the existing fail-open capture seam, off the response path, identifiable by prediction id — matching the non-streamed contract (FR-356/SC-180). **Scope note (Codex):** the generated `prediction_id` MUST be delivered to the client (e.g. an initial metadata SSE event), reconciled with the "never alters the stream" rule — the label API takes a *caller-supplied* ID and there is no prediction-list endpoint, so without exposing the ID the streamed caller cannot attach the promised delayed label (SC-180 unreachable).
- [ ] **T611** [P] [US4] Write `tests/test_stream_capture.py` — web-free seam: a streamed completion yields the same log/capture rows as non-streamed; never blocks/alters the stream.

**Checkpoint**: streamed predictions can be labeled and enter quality/shadow.

---

## Phase 7: User Story 5 — Live HPO progress (Priority: P4)

- [ ] **T612** [US5] Surface live per-trial HPO progress (completed trials + objective values) in `ui/app/training` (FR-357/SC-181). **Scope note (Codex):** there is no live trial state to read today — `run_study` keeps `trials_log` local and returns it only after `optimize` finishes; `JobManager` writes only the final summary. So this task MUST FIRST add a **backend progress sink** — an injected trial-completion callback exposed through the agent + gateway — and only then consume it from the console (still dependency-light; no external Optuna dashboard service).
- [ ] **T613** [P] [US5] Test the progress surface (trials appear/update; dependency-light).

**Checkpoint**: operators can watch a study run.

---

## Phase 8: User Story 6 — Shadow-replay console UI (Priority: P4)

- [ ] **T614** [US6] Add a console surface (`ui/app/models`) to dispatch shadow-replay and read its advisory verdict via the existing `POST /models/{name}/shadow-replay` + verdict endpoints; mark verdicts clearly advisory/never-gating (FR-358/SC-182). **Scope note (Codex):** MUST also add both `POST /models/{name}/shadow-replay` and `GET /models/{name}/shadow-replay/{id}` to `ui/lib/gw-allowlist.ts` — neither is currently allowlisted, so the BFF returns 404 before key injection; test through the real allowlist.
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
