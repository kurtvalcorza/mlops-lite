# Tasks: Score-at-Registration (closing SC-068)

**Input**: Design documents from `specs/015-on-demand-version-loading/` (spec.md, plan.md, research.md,
data-model.md, contracts/evaluate-guard.md, quickstart.md).

> **Status (2026-06-30):** **BUILT (offline) — on-hardware SCs pending Kurt's GPU box.** All code +
> offline unit tests landed and green (134 passed / 31 skipped, no regression). The on-hardware SCs
> (SC-087/088/089/090/091/092, the real llama-server/whisper-cli/torch scoring + `nvidia-smi`
> one-model-in-VRAM checks, and the live 2-trial HPO study) must run on the RTX 5070 Ti box — they skip
> cleanly offline. IDs continue the shared space (T276+).

## Format: `[ID] [P?] [Story] Description`

- **[P]** = parallelizable (different files, no dependency). `[USx]` maps to the spec's user stories.

---

## Phase 1: Setup — benchmark fixtures + metric finalization (D4)

- [x] **T276** [P] Add the embeddings held-out fixture `benchmarks/embedding/recall_smoke.jsonl` (tiny,
  `{query, relevant_ids/positives}` shape for recall@k); content-hashed at load.
- [x] **T277** [P] Add the ASR held-out fixture `benchmarks/asr/wer_smoke.jsonl` (`{audio_b64, text}`,
  tiny); content-hashed at load.
- [x] **T278** Finalize `embedding`/`asr` defaults in `gateway/app/evaluation.py`
  (`DEFAULT_BENCHMARKS` + `METRICS`): wire `embedding→recall@k` (higher-better) and `asr→wer`
  (lower-better) to the new fixtures; confirm directions. Update `benchmarks/README.md` (drop "stub").

**Checkpoint**: all four modalities have a committed, content-hashed held-out benchmark + a finalized
metric+direction.

---

## Phase 2: Foundational — the in-process scoring module (BLOCKS all stories)

- [x] **T279** Create `training/scoring/__init__.py` with `score_and_log(name, version, modality,
  predict_fn, *, client=None)`: load the modality benchmark (repo path — reachable from the native
  trainer), run `predict_fn(rows, modality, version)`, compute the metric via `evaluation.metric_for`,
  and log it on the new version via `evaluation._log_eval`. Assumes the **caller holds the GPU lease**.
- [x] **T280** [P] In-process **vision** scorer (`training/scoring/vision.py`): run the in-memory torch
  model over the benchmark images → top-1 labels.
- [x] **T281** [P] In-process **embeddings** scorer (`training/scoring/embeddings.py`): encode with the
  in-memory sentence-transformers model → vectors → recall@k.
- [x] **T282** [P] Transient **llama.cpp** LLM scorer (`training/scoring/llm.py`): load base GGUF +
  LoRA-GGUF adapter (the adapter the flow just produced) in a short-lived llama.cpp, generate over the
  QA prompts → answers; **load → score → free** (D5). *(Resolve llama-cli vs short-lived llama-server.)*
- [x] **T283** [P] Transient **whisper.cpp** ASR scorer (`training/scoring/asr.py`): transcribe the WER
  fixture via the served ggml in a short-lived whisper.cpp → text; **load → score → free** (D6).
- [x] **T284** Lease-hold discipline (`training/trainer.py` / flow glue): scoring runs **inside the
  fine-tune's existing lease hold** — the training model (+optimizer) is **freed** (`empty_cache`) before
  any served-artifact scorer loads; never two models resident; release **once** after scoring (D7, FR-140).

**Checkpoint**: a held-lease caller can score any modality in-process and log the metric; one model in
VRAM at any instant.

---

## Phase 3: User Story 1 — every fine-tune born with its metric (P1) 🎯 MVP

**Goal**: each fine-tune registers a version that already carries its eval metric (FR-137/138/139).

**Independent Test**: fine-tune two distinct versions → each registers with a distinct logged metric;
`nvidia-smi` shows ≤ one model resident through train→score.

- [x] **T285** [P] [US1] `tests/test_score_at_registration.py` — per-modality `score_and_log` logs a
  version metric over a tiny fixture with an injected `predict_fn` (offline; assert tags + direction).
- [x] **T286** [US1] Wire `score_and_log` into `training/flows/finetune.py` (LLM) **after** GGUF
  convert + register, **before** lease release (uses the T282 scorer).
- [x] **T287** [P] [US1] Wire into `training/flows/vision.py` (T280 scorer).
- [x] **T288** [P] [US1] Wire into `training/flows/embeddings.py` (T281 scorer).
- [x] **T289** [P] [US1] Wire into `training/flows/asr.py` (T283 scorer).
- [x] **T290** [US1] Scoring-failure policy: a fine-tune whose **training** succeeds but **scoring** fails
  registers the version + **warns** (the gate's missing-metric policy, PR #20, then applies) — does not
  fail the whole run (spec Edge Case).
- [ ] **T291** [US1] On-hardware: one real fine-tune per modality registers with a logged metric; VRAM
  one-model check (**SC-087, SC-088, SC-092**).

**Checkpoint**: every fine-tune is born evaluated.

---

## Phase 4: User Story 2 — meaningful HPO objective + compare (P1)

**Goal**: HPO optimizes toward the genuinely best trial; `compare` judges from logged metrics; finding #4
gone (FR-141/142).

**Independent Test**: a 2-trial study logs **distinct** objectives + registers the real best, with no
hostname error.

- [x] **T292** [P] [US2] Test: the HPO objective reads the **trial's own registered metric** (no daemon
  call); `compare` reads both versions' logged metrics with no reload (injected fakes).
- [x] **T293** [US2] `training/flows/hpo.py`: objective = the trial's registered version's logged metric
  (produced by US1's `score_and_log`); **remove** the live-predictor-over-HTTP objective path so no
  `host.docker.internal` call occurs (closes finding #4).
- [x] **T294** [US2] `gateway/app/evaluation.py` `compare()`: judge from **logged metrics** of champion +
  challenger (no sequential model reload); keep the metric/direction math unchanged.
- [ ] **T295** [US2] On-hardware: 2-trial HPO study → distinct objectives, best registered, **no**
  `Name or service not known` (**SC-089, SC-090**).

**Checkpoint**: HPO + compare are correct, not degenerate.

---

## Phase 5: User Story 3 — gateway eval guard (P2)

**Goal**: gateway `/evaluate` never silently scores the resident model for a different requested version
(FR-143).

**Independent Test**: `/evaluate` for a non-`@serving` unscored version → clear error, not a score.

- [x] **T296** [P] [US3] `tests/test_eval_guard.py` — unscored non-serving version → clear error; a
  version with a logged metric → returns/uses it; `@serving` version → still scores (per contract).
- [x] **T297** [US3] Implement the guard in `gateway/app/routers/models.py` + a helper in
  `gateway/app/evaluation.py` per `contracts/evaluate-guard.md` (requested ≠ `@serving` AND no logged
  metric → clear `409/422` error).
- [ ] **T298** [US3] On-hardware: guard returns the error (not a score); `/compare` reads logged metrics
  (**SC-091**).

**Checkpoint**: the operator surface is correct or refuses — no wrong-model scoring.

---

## Phase 6: Polish & cross-cutting

- [x] **T299** [P] Docs: README (new "score-at-registration" behavior + the two fixtures), `benchmarks/
  README.md` (embedding/asr no longer stubs); flip spec/plan/tasks Status → BUILT.
- [x] **T300** No-regression: full **001–014** suite green (GPU-tenant tests validated in isolation per the
  single-lease behavior) (**SC-093**).
- [x] **T301** [P] Confirm **no new dependency** (the scorers reuse torch / built llama.cpp+whisper.cpp /
  pure-Python metrics); update the deps note.
- [ ] **T302** PR + **dual-bot review loop** (`@claude` + `@codex`) → fix → merge when clean.

---

## Dependencies & Execution Order

- **Phase 1 (fixtures/metrics)** → no deps; start immediately. Blocks scoring for embeddings/asr.
- **Phase 2 (scoring module)** → depends on Phase 1; **BLOCKS all user stories**.
- **US1 (Phase 3)** → after Phase 2. The MVP — everything else reads what it produces.
- **US2 (Phase 4)** → after US1 (the HPO objective + compare read US1's logged metrics).
- **US3 (Phase 5)** → after Phase 2; independent of US1/US2 (pure gateway guard) — can parallel US1/US2.
- **Polish (Phase 6)** → after the user stories.

### Parallel opportunities

- T276/T277 (fixtures) in parallel; T280–T283 (per-modality scorers) in parallel; T287–T289 (vision/
  embeddings/asr flow wiring) in parallel after T286's pattern lands.

## Notes

- Keep the GPU stack frozen and add no dependency (Principle III). Scoring stays **inside** the fine-tune's
  lease hold (Principle II) — free the training model before any served-artifact scorer loads.
- Validate GPU-tenant tests **in isolation** (the single lease serializes them — expected, not a regression).
- Batch (014) is **out of scope** — do not change its `@serving` scoring.
