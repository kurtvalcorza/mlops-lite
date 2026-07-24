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

- [ ] **T595** Confirm no change in this feature needs a schema migration; if one emerges, create it as a NEW numbered `platformlib/migrations/*.sql` (FR-359 — independently, a contract update is required only if an external API/endpoint also changes; a schema-only internal change needs no contract edit) before dependent tasks.

---

## Phase 3: User Story 1 — Batch inference correctness (Priority: P1) 🎯 core

**Goal**: batch scores the requested version under the lease (or refuses); every admitted batch modality has a real path.

**Independent Test**: a batch for a non-resident version scores that version (offline ordering + injected predict_fn); an ASR batch completes or is rejected at submission.

- [ ] **T596** [P] [US1] Write `tests/test_batch_version_assert.py` — offline: a batch requesting version A while B is "resident" asserts/loads A before scoring (injected predict_fn + fake admission), never scores B; refuses cleanly if a job holds the GPU (FR-348/FR-350). MUST also assert the **restore/unload** of the temporary target after the batch — on BOTH a successful batch and a batch that raises mid-scoring — so the prior desired target is left resident (Codex — see T597). Add two more cases (Codex round-5): (i) a **load/OOM failure** (target never becomes resident) still restores the prior target — the load sits inside the restore scope; (ii) a **concurrent online `/infer`** issued mid-batch is queued/refused and never served the temporary version (batch-wide exclusion, FR-350).
- [ ] **T597** [US1] In `training/flows/batch_infer.py`, load/assert the requested `model`/`registry_version` under admission before scoring (once per batch, not per record); refuse without preempting a running job (FR-348/FR-350; closes the explicit-`registry_version`-honoring gap — NOT 015's SC-068, which kept batch-vs-`@serving` scoring correct). **Scope note (Codex):** this CANNOT be done in `batch_infer.py` alone — the engine endpoints accept no requested model/version and the only targeted reload (serving-LLM pointer) is refused once `_gpu_batch_active`. The substantive work is an **agent-side target-version load/assert seam** (host agent + engine wiring) called before scoring; `batch_infer.py` only drives it. **Restore requirement (Codex):** the batch drives the SAME resident engine online `/infer` uses (`batch_infer.py` docstring §Lease discipline), so loading version A for the batch leaves A serving online traffic until the next reload/idle-release. The load/assert MUST be paired with a `finally` that **restores the prior desired/resident target (or unloads the temporary one)** on both success and exception — never leave the batch's temporary version serving online requests. **Load-inside-restore + exclusion (Codex round-5):** the `ensure resident` load MUST sit INSIDE the `try` (a spawn/readiness/OOM failure that already disturbed the prior engine still hits the restore), and the batch MUST hold a **batch-wide exclusion** that queues/refuses online `/infer` for the temporary target's lifetime — `_gpu_batch_active` only blocks eviction, not inference, so without it an online call between rows is served the wrong version (FR-350). **Tabular batch path (Codex round-5):** tabular is admitted but its batch path is broken — `batch_infer.py` posts `{"features": row}` while the child requires `{"rows": [...]}` (`serving/children/tabular_service.py:99-127`), so every tabular row 422s. Fix the payload (post `{"rows": [row]}`, or batch rows) so tabular batch scores instead of failing (FR-349). **Per-modality version binding (Codex round-6):** the version-honoring seam is NOT LLM-only — only `LlamaAdapter` has registry binding/loaded-identity; the vision child loads the static `VISION_MODEL_KEY` and tabular's `_resolve_object()` resolves the configured `NAME@serving`, so neither accepts the batch's `model`/`registry_version` today. Without a binding surface for vision AND tabular, an implementation would write the requested version into the result manifest while scoring a static/current artifact — the exact silent-wrong-version bug, just relocated. Add the per-modality target-binding surfaces and test a **non-serving requested version for every admitted modality and alias**, not one generic fake. **Self-exclusion bypass (Codex round-6):** the batch's own rows post to the same `/engines/*` paths as online traffic in separate agent threads, so the FR-350(b) exclusion needs an authenticated batch marker/token (or an agent-internal scoring seam) or it deadlocks the batch against itself — test that batch rows proceed while unrelated online requests are excluded. **Re-read desired on restore (Codex round-6):** the `finally` restore MUST re-read the latest desired target (a promote can land mid-batch while its reload is deferred by `_gpu_batch_active`), not blindly restore the snapshot — test a promotion arriving mid-batch is preserved (FR-350).
- [ ] **T598** [US1] ASR batch (**corrected, Codex**): ASR is ALREADY rejected at submission — `hostagent/jobs.py` validates against `BATCH_MODALITIES` (which excludes `asr`); `GPU_BATCH_MODALITIES` (which lists `asr`) is only serving-holder protection, consulted *after* validation. So there is **no accepted-then-failed bug** to fix. This task is therefore net-new-only: **if** batched ASR is wanted, add `asr` to `BATCH_MODALITIES` + a real ASR path in `batch_infer.py`; **otherwise drop this task** — no action needed (FR-349/SC-176 revised accordingly, or descoped). **GPU-alias protection (Codex round-5, separate from ASR):** `BATCH_MODALITIES` admits the aliases `text-generation` and `image-classification` (both routed by `batch_infer.py`), but `GPU_BATCH_MODALITIES` lists only `llm`/`vision`/`asr` — so an alias-named GPU batch leaves `_gpu_batch_active` False and gets NO eviction protection (a promote reload or preempting request can change the engine mid-batch → mixed-version results). Normalize the aliases to their GPU modalities (`text-generation`→`llm`, `image-classification`→`vision`) before the `GPU_BATCH_MODALITIES` membership check, and test that BOTH alias-named batches set the protection flag.
- [ ] **T599** [HW] [US1] On the RTX 5070 Ti box: validate the load-under-lease leg — a batch for a non-resident version scores it correctly while preserving one-GPU-tenant (SC-175). **Must exercise the two highest-risk FR-350 guarantees on real hardware (Codex round-6):** (i) a **load/OOM failure** leaves the prior serving target resident again (the fake-admission T596 cannot exercise real lifecycle teardown/readiness/OOM); (ii) a **concurrent online `/infer`** during the batch is excluded (never served the temporary version) while the batch's own rows proceed; and (iii) a **promote landing mid-batch** is preserved by the re-read-on-restore. Update the `quickstart.md` HW recipe to cover these, not just the successful load.

**Checkpoint**: batch is correct for every admitted modality; ships as its own PR.

---

## Phase 4: User Story 2 — Tabular full modality (Priority: P2) 🎯 core

**Goal**: tabular can train→gate→serve→monitor→retrain like vision — CPU/off-lease, no heavy dep.

**Independent Test**: a tabular dataset fine-tunes → registers-with-metric → gates on a committed AUC fixture → promotes → serves; quality window scorable where labels exist.

- [ ] **T600** [P] [US2] Add `benchmarks/tabular/auc_smoke.jsonl` — a committed held-out tabular eval fixture.
- [ ] **T601** [P] [US2] Write `tests/test_tabular_eval.py` — web-free: the tabular **prediction factory** + the existing pure-Python `auc` metric + gate over the fixture (AUC promoted from stub to committed metric) (FR-352).
- [ ] **T602** [US2] Add `training/scoring/tabular.py` as the tabular **prediction factory** — a `predict_fn(rows, modality, version)` returning probability scores, the collaborator `training/scoring/__init__.score_and_log` requires (mirroring the vision/embeddings scorers). **Do NOT re-implement AUC** — the pure-Python `auc` metric already exists at `gateway/app/evaluation.py:153-173` and is in `METRICS`; T604 only promotes it from stub (FR-352).
- [ ] **T603** [US2] Add `training/flows/tabular_finetune.py` — CPU LightGBM fine-tune mirroring `vision_finetune.py`: train → register version with tabular task/engine tags + logged metric → failure cleanup (no partial version) (FR-351/FR-354); register in `flow_dispatch`. **Scope note (Codex):** registering in `flow_dispatch` is NOT sufficient — `platformlib.topology.TRAINABLE_MODALITIES` excludes tabular and the `finetune` job kind is `gpu=True`, so admission work is needed to admit tabular training as **CPU/off-lease** (no GPU acquisition, FR-354). **HPO-leak (Codex):** `TRAINABLE_MODALITIES` is the shared `modality_set` for BOTH the `finetune` AND `hpo` kinds (`hostagent/jobs.py` KINDS, both `gpu=True`), so naively adding `tabular` to that tuple would also make `/study` accept a tabular HPO job, acquire the GPU lease, and then fail every trial with `SearchSpaceError` (no tabular search-space in `training/search_spaces.py`/`hpo.py`). Admit tabular via a **per-kind** modality policy — tabular for `finetune` only, CPU/off-lease — NOT by extending the shared tuple; OR fully wire tabular HPO and keep it off-lease too. ALSO the tabular child serves the single configured `TABULAR_MODEL` name via `@serving`, so a version trained under any other output name won't be served — constrain/validate the output name to the served name (or add dynamic tabular serving-selection + reload) before claiming train→promote→serve parity. **Warm-reload (Codex round-5):** even with the output name constrained, a promotion is NOT picked up while the child is warm — `tabular_service.py:77-86` re-resolves `@serving` only when `_bundle is None`, and continuous traffic refreshes `_last_used` so idle-release never fires; production keeps serving the old booster after the alias moves. T603 MUST add version-aware invalidation/reload of the tabular child on promote (not rely on idle-release), with a **warm-v1 → promote-v2 → predict-v2** test (FR-351/SC-177). **All promote paths (Codex round-6):** the reload MUST cover EVERY alias-moving caller, not just the operator `/models/{name}/promote` route — the scheduler's auto-on-green path (`scheduler.py:566-569` `_default_promote`) and suggestion acceptance (`routers/policies.py:124-125`) call `registry.promote` directly, bypassing the operator go-live. Put the version-detection/invalidation at a **shared alias/child boundary** (or add explicit handling), and add warm→promote→predict tests for the automatic and suggestion paths too, or those promotions leave the warm child on the old booster indefinitely. **Benchmark wiring order (Codex round-6):** `training/scoring.score_and_log()` calls `evaluation.load_benchmark(modality)` with no override and `DEFAULT_BENCHMARKS` has no `tabular` entry, so under the T600–T602 → T603 → T604 order the flow's registration-time scoring raises "no default benchmark", the wrapper swallows it, and the version registers WITHOUT the logged AUC that FR-351 promises (letting the missing-metric gate decide promotion). Adding the fixture file in T600 does NOT make it discoverable — make T604's `DEFAULT_BENCHMARKS`/`METRICS` wiring a **prerequisite of T603**, or have T603 pass the fixture explicitly.
- [ ] **T604** [US2] Promote tabular AUC from stub to a committed metric in `gateway/app/evaluation.py` (METRICS + live serving predictor path) (FR-352).
- [ ] **T605** [US2] Wire tabular into quality monitoring (`gateway/app/quality.py`) so it can drive breach→retrain — this is MANDATORY, not optional (FR-353/SC-178): a documented exclusion is limited to *individual requests with no supplied label*, never the tabular modality as a whole. **Scope note (Codex):** editing `quality.py` alone is insufficient — the tabular `/predict` router never calls `quality.log_prediction`, returns no per-row prediction IDs, and the tabular child reports no registry version, so there are no version-scoped prediction rows for `quality.window()`. This task MUST also add the tabular serving/router **prediction-logging + identity contract** (per-row prediction IDs + the actually-served version) first. Each row MUST log the **numeric probability `score`** (not the response dict, not the thresholded class) — `quality.score_window` feeds stored values straight to `evaluation.auc`, which ranks numeric scores (a dict breaks sorting; the binary class discards the ranking AUC measures). **Scheduler-retrain gap (Codex):** to actually *drive* breach→retrain, tabular MUST also be added to `gateway/app/scheduler.py`'s `MODALITY_TASK` map (line 50-51). Today `scheduler.py:412` does `MODALITY_TASK[policy["modality"]]` (a direct index, not `.get`), so a tabular policy tick raises `KeyError`, which the tick swallows as `check_error` — a breach is never detected and the promised retrain never launches. Add the `tabular` mapping AND a tabular policy-tick test that asserts a breach is detected (not swallowed as a check error). **Policy/retrain validators (Codex round-5):** the mapping alone still can't launch a tabular retrain — `platformlib/contracts.py:161-170` (`ModelPolicy.validate`) and `gateway/app/routers/monitor.py:47-52` (`RetrainSpec._known_modality`) both reject any modality not in `TRAINABLE_MODALITIES`, so a tabular policy returns 400 and a manual retrain 422 *before* the scheduler runs. Since T603 keeps tabular OUT of the shared `TRAINABLE_MODALITIES` tuple (HPO-leak), this task MUST decouple those validators to admit tabular as a trainable-*policy* modality (a `finetune`-only/CPU set, distinct from the HPO admission set) and test policy-create + retrain-dispatch accept tabular. **Contract (Codex round-6/FR-359):** adding per-row prediction IDs + the served registry version changes the external gateway `/predict` schema AND the agent `/engines/tabular/predict` response (`specs/020-stack-remediation/contracts/children-api.md:11` currently describes it as predictions-only JSON) — like T608/T610/T612, land the FR-359 contract update for this identity-fields change so the normative API doesn't stay stale.
- [ ] **T606** [P] [US2] Write `tests/test_tabular_finetune.py` — seam-level (dispatch, register-with-metric, failure cleanup); full CPU run live-gated.
- [ ] **T607** [US2] Confirm no heavy dependency entered the gateway/agent images and tabular holds no GPU lease (FR-354/FR-360).

**Checkpoint**: tabular is a full lifecycle modality; ships as its own PR.

---

## Phase 5: User Story 3 — Dataset byte-download in the console (Priority: P3)

- [ ] **T608** [US3] Add a gateway/BFF **byte-proxy** dataset download path (`gateway/app/routers/datasets.py` + `ui/app/data`) so an operator downloads bytes without object-store creds reaching the browser (FR-355/SC-179). **Scope note (Codex):** (a) it MUST be a byte **proxy**, NOT a bare presigned URL — the presigned URL is generated from the internal S3 endpoint (`garage:3900`, `datasets.py:130-134`) the browser cannot resolve (exactly why the existing `download_url` is unusable). (b) MUST also add the exact download route to `ui/lib/gw-allowlist.ts` — the BFF rejects any unmatched route before injecting the key (current dataset entries allow only the manifest GET), so without the allowlist entry the browser gets a BFF 404 even when the gateway endpoint works. (c) **Strip the presigned URL from the browser (Codex round-5):** `datasets.py:118-136` still adds a presigned `download_url` to *every* version manifest, and `ui/app/data/page.tsx:124-137` fetches that manifest through the BFF — so the browser keeps receiving the signed object-store capability even if it never clicks download. Retire/strip `download_url` from the browser-facing manifest so NO presigned URL reaches the browser on any data-page response. (d) **Contract (FR-359):** the new byte-download endpoint is an external API addition — land a contract update for it.
- [ ] **T609** [P] [US3] Test the download path (credential never in the browser payload; correct bytes/manifest) — and assert NO presigned URL appears in ANY data-page response (the version manifest included), not only the new byte endpoint (Codex round-5).

**Checkpoint**: data stage is fully operable from the console.

---

## Phase 6: User Story 4 — Streamed-prediction logging (Priority: P3)

- [ ] **T610** [US4] Capture predictions served over `/infer/stream` (`gateway/app/routers/stream.py`) via the existing fail-open capture seam, off the response path, identifiable by prediction id — matching the non-streamed contract (FR-356/SC-180). **Scope note (Codex):** the generated `prediction_id` MUST be delivered to the client (e.g. an initial metadata SSE event), reconciled with the "never alters the stream" rule — the label API takes a *caller-supplied* ID and there is no prediction-list endpoint, so without exposing the ID the streamed caller cannot attach the promised delayed label (SC-180 unreachable). **Contract (Codex round-5/FR-359):** the initial metadata SSE event is an intentional change to the `/infer/stream` shape, but `specs/022-registry-driven-llm-serving/contracts/serving-resolution.md:63-66` still requires the stream bytes to remain unchanged — update that contract (or add a 025 contract delta) so the normative API description matches the new frame before this lands.
- [ ] **T611** [P] [US4] Write `tests/test_stream_capture.py` — (a) web-free seam: a streamed completion yields the same log/capture rows as non-streamed, off the response path. (b) **Router/client-level stream test (Codex/FR-356):** parse the actual SSE stream, assert the initial metadata event carries the generated `prediction_id`, and *separately* assert the upstream start/token/done frames are byte-intact. A seam-only test passes even if T610 logs an id without injecting it into the stream — leaving delayed labeling unreachable (SC-180) — so the client-facing assertion is required, not optional.

**Checkpoint**: streamed predictions can be labeled and enter quality/shadow.

---

## Phase 7: User Story 5 — Live HPO progress (Priority: P4)

- [ ] **T612** [US5] Surface live per-trial HPO progress (completed trials + objective values) in `ui/app/training` (FR-357/SC-181). **Scope note (Codex):** there is no live trial state to read today — `run_study` keeps `trials_log` local and returns it only after `optimize` finishes; `JobManager` writes only the final summary. So this task MUST FIRST add a **backend progress sink** — an injected trial-completion callback exposed through the agent + gateway — and only then consume it from the console (still dependency-light; no external Optuna dashboard service). **Contract (FR-359):** the new/extended agent+gateway progress surface is an external API change — land a contract update for it.
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
