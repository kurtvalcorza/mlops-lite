---
description: "Task list for Hyperparameter Optimization — Optuna + MLflow (012)"
---

# Tasks: Hyperparameter Optimization (Optuna + MLflow)

**Input**: Design documents from `specs/012-hyperparameter-optimization/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the fine-tune path
(`POST /runs` → native trainer → `finetune_flow`) and **011's evaluation harness** (the optimization
objective — hard dependency). Adds HPO additively; the single-value fine-tune path is unchanged.

**Tests**: Re-run the relevant 001–011 integration suite per phase on the target GPU machine before the
next. Task IDs continue the shared space (T222+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-29):** **BUILT** (US1–US3). Optuna study runner (`training/flows/hpo.py`) +
> per-modality search spaces (`training/search_spaces.py`) + trainer `/study` + gateway `/studies`
> proxy + minimal Runs-UI surface (optimize toggle + study status + best-trial). New dep `optuna==4.9.0`
> (verified NO torch-family movement). Trials strictly sequential (`n_jobs=1`); objective = 011's eval
> metric; best trial registered + tagged, eligible for 011's gated promotion. **GPU/FT stack FROZEN.**
> No constitution amendment. Tasks T222–T237.
>
> **Build note — decisions (recorded):**
> - **`optuna==4.9.0` (T222/T223):** pure-Python, server-less, in-memory study store; confirmed it
>   installs without moving the frozen torch/transformers/peft pins. No Ray, no daemon (Principle III).
> - **Search spaces (T229):** LLM (`lora_r`∈{8,16,32,64}, `lora_alpha`∈{16,32,64}, `lr` loguniform
>   1e-5..5e-4, `steps` int 10..60) + vision (`lr` loguniform, `epochs` 1..5, `unfreeze_epochs`∈{0,1,2})
>   committed; ASR/embeddings carried as stubs. Per-study `overrides` narrow/replace any knob.
>   `finetune_flow` gained additive optional `lora_alpha`/`lr` (defaults reproduce prior behavior — the
>   single-value `/runs` path is unchanged, SC-074).
> - **Objective (T230):** the trial scalar is **011's eval harness** output via an injectable `eval_fn`;
>   study direction is resolved from 011's metric registry by modality (maximize accuracy, minimize
>   WER). A broken eval → **failed** trial (not worst-valid). **Inherits 011's SC-068 limitation** — the
>   live serving path scores the resident model, so on-demand per-version scoring is the on-hardware
>   step; documented in `hpo._default_eval`, and the tests inject `eval_fn`.
> - **Winner-only registration (T232):** each trial's `finetune_flow` registers a version (verbatim
>   reuse); on completion HPO **tags the best trial's version** (study id + winning params + metric, so
>   it flows into 011's gate) and **deletes every other version this study created** — exactly one
>   survives, no registry flood (FR-116). Failed/eval-failed trials leave no surviving version.
> - **Pruning (T225):** **full trials by default** (no pruner). `run_study(pruner=…)` exposes Optuna's
>   `MedianPruner` as the opt-in seam; activating it needs the optional `trial.report()`/`should_prune()`
>   mid-train hook (not wired by default — loss is an imperfect proxy for 011's eval metric).
> - **Per-trial CUDA isolation:** the default `train_fn` runs each trial as a fresh `run_flow.py`
>   subprocess (same isolation the trainer daemon uses); the study holds the single GPU lease for its
>   duration and a study/run can't co-reside (shared `_active` gate + 409).
>
> **Firm decisions (captured, pre-grill):**
> 1. **Optuna, not Ray Tune** — server-less, in-process (like ephemeral Prefect); one light dep, no
>    daemon. Ray Tune rejected (heavy cluster runtime) in plan.md → Complexity Tracking.
> 2. **Trials sequential on the one GPU** (`n_jobs=1`) — each trial is a one-model-in-VRAM lease tenant;
>    total wall-clock = `n_trials × per-train-time`. Principle II untouched.
> 3. **A trial = the existing `finetune_flow`** with sampled params (reuse, don't fork the trainer).
> 4. **Objective = 011's eval metric** (hard dependency on 011), optimized in the metric's direction.
> 5. **Best trial → one registered MLflow version** (tagged study id + winning params + winning metric),
>    eligible for 011's gated promotion; losing trials register nothing.
> 6. **Each trial = an MLflow child run** under a parent (study) run.
> 7. **Additive** — single-value `POST /runs` path + VRAM mutex unchanged.
>
> **Grilled decisions (2026-06-28):**
> 1. **Search spaces = sensible default ranges per modality, configurable** (mirrors 010/011; not pinned).
>    LLM-LoRA (`lora_r` ∈ {8,16,32,64}, `lora_alpha` tied or {16,32,64}, `lr` ∈ loguniform(1e-5, 5e-4),
>    `steps`/epochs small range) & vision (`lr` ∈ loguniform, `epochs` small range, `freeze-depth`
>    categorical); ASR/embeddings = guidance stubs (land with 010). (FR-115, T229)
> 2. **Trial budget = small default `n_trials` (~15) + optional `timeout` cap, both configurable** (each
>    trial is a full train; wall-clock ≈ `n_trials × per-train-time`). (FR-112, T224)
> 3. **Full trials by default; `MedianPruner` opt-in** — the `trial.report()`/`should_prune()` hook is
>    OPTIONAL/opt-in, not the default path (loss is an imperfect proxy for 011's eval). Rationale in
>    plan.md → Complexity Tracking. (FR-113, T225)
> 4. **Gateway/CLI core + minimal Runs-UI** — "optimize" toggle on the Runs launch form + study status +
>    best-trial (params + winning metric); full live per-trial UI = fast-follow (Non-Goals). (FR-117, T234)

---

## Phase 0 — Pre-flight (gates everything)

- [x] **T222** [US1] Confirm `optuna` (latest stable) installs clean in the training venv **without
  perturbing the frozen torch/transformers/peft/accelerate/datasets pins** (dry `pip install optuna`;
  verify no torch-family movement). Confirm **011's eval harness** exposes a callable that returns a
  scalar objective + its optimize direction (max/min) for a trained candidate. (FR-111, FR-114)

## Phase 1 — Optuna study runner wrapping the trainer (US1, P1) → SC-070

- [x] **T223** [US1] `training/requirements.txt`: add `optuna`; leave the **frozen** GPU/FT stack
  untouched; record the pin in `scripts/native_env.lock`. (FR-111, FR-118)
- [x] **T224** [US1] NEW `training/flows/hpo.py`: Optuna study runner — `optuna.create_study(direction=…)`,
  `study.optimize(objective, n_trials=N, timeout=…, n_jobs=1)`; the **objective** samples a hyperparameter
  set and invokes the **existing** `finetune_flow(...)` (one real training run). **Sequential only**
  (`n_jobs=1`); each trial frees VRAM (the flow's `del`/`empty_cache` discipline) before the next. Trial
  budget defaults to a **small `n_trials` (~15)** with an **optional wall-clock `timeout`** cap; **both
  configurable** per study. (FR-111, FR-112)
- [x] **T225** [US1] MLflow parent/child tracking in `hpo.py`: open a **parent (study) run**
  (`mlflow.start_run()`), and per trial open a **nested child run** (`start_run(nested=True)`) logging the
  sampled params + the trial objective. **Failed trial** → record failed/pruned, register **NO** partial
  version, free the GPU, **continue** the study (raise `optuna.TrialPruned` / handle the exception).
  **Default: every trial runs to completion — no pruner.** Expose Optuna **`MedianPruner` as opt-in
  config** (off by default); when enabled, wire the **OPTIONAL** `trial.report()`/`should_prune()` hook
  (an opt-in mid-train signal, not the default path — loss is an imperfect proxy for 011's eval). (FR-113)
- [x] **T226** [US1] `training/trainer.py`: add `POST /study` (launch — one study at a time on the lease,
  respects the existing serving-resident **409** one-model-in-VRAM gate) + `GET /study/{id}` (status:
  trials, per-trial objective, best, study state), mirroring the existing `/train` endpoints. (FR-111, FR-112)
- [x] **T227** [US1] `gateway/app/routers/runs.py`: proxy `POST /studies` + `GET /studies/{id}` to the
  trainer's `/study` (mirroring the `/runs` proxy: 503 unreachable, 409 busy, 502 trainer-error), with a
  `gateway_run_ops`-style counter. (FR-111)
- [x] **T228** [P] [US1] `test_hpo_study`: a study with `n_trials=3` runs **exactly 3 trials
  sequentially** (never two models resident — assert via the trainer/GPU-free signal), each an MLflow
  **child run** under one **parent (study) run**; a deliberately-failing trial is recorded and the study
  still finishes with a best trial. (SC-070)

## Phase 2 — Per-modality search spaces + objective wired to 011 (US2, P1) → SC-071 + SC-072

- [x] **T229** [US2] NEW `training/search_spaces.py`: declare **per-modality** search spaces with
  **sensible default ranges** that are **configurable / user-overridable** (mirrors 010/011) — **LLM-LoRA**
  (`lora_r` ∈ {8,16,32,64}, `lora_alpha` tied or {16,32,64}, `lr` ∈ loguniform(1e-5, 5e-4),
  `steps`/epochs small range) and **vision** (`lr` ∈ loguniform, `epochs` small range, `freeze-depth`
  categorical); the sampler picks the space by the study's modality. Carry **ASR/embeddings** spaces as
  **guidance stubs** (implemented when those fine-tune paths land in 010). (FR-115)
- [x] **T230** [US2] Wire the trial **objective** in `hpo.py` to **011's eval harness**: after
  `finetune_flow` registers the candidate, run 011's eval and return its scalar; `create_study(direction=…)`
  set from the metric's optimize direction. If 011's eval **errors** for a candidate, mark the trial
  **failed** (not scored worst-valid). (FR-114, FR-115)
- [x] **T231** [P] [US2] `test_hpo_objective`: an LLM-LoRA study samples within the LLM space (only
  LLM knobs), each trial's objective **equals 011's eval output** for that candidate, and Optuna's
  `best_trial` is the best eval metric (correct direction); a vision study uses the vision space and not
  the LLM knobs. (SC-071, SC-072)

## Phase 3 — Best trial → register + Runs-UI surface (US3, P2) → SC-073

- [x] **T232** [US3] In `hpo.py`, on study completion register **only the best trial** as a new MLflow
  **model version** via the existing `register_version` path, tagged with the **study id**, the **winning
  hyperparameters**, and the **winning eval metric**; losing trials register **no** version. (FR-116)
- [x] **T233** [US3] Confirm the registered best version is a valid candidate for **011's gated
  promotion** (it flows into the existing eval-gated promotion path — same tags/shape the gate expects).
  (FR-116)
- [x] **T234** [US3] `ui/` Runs tab: add the **minimal HPO surface** — an **"optimize" toggle** on the
  Runs launch form + a display of **study status + best-trial** (winning params + winning metric);
  consume the gateway `/studies` + `/studies/{id}` surface. **Full live per-trial visualization is a
  fast-follow** (Non-Goals), out of 012's scope. (FR-117)
- [x] **T235** [P] [US3] `test_hpo_register`: after a study completes, **exactly one** model version
  (the best trial) is registered with study-id + winning-params + winning-metric tags, losing trials
  register none, and 011's gated promotion accepts the version; the minimal Runs-UI surface (optimize
  toggle + study status + best-trial) renders. (SC-073)

## Phase 4 — Cross-cutting regression

- [x] **T236** [P] No-regression: the existing single-value `POST /runs` fine-tune path, the trainer's
  one-run-at-a-time + VRAM mutex, and the full 001–011 keyed sweep behave unchanged; idle footprint
  unchanged (no resident `optuna` process — server-less). (SC-074)
- [x] **T237** Commit `optuna` pin (`requirements.txt` + `native_env.lock`), `flows/hpo.py`,
  `search_spaces.py`, the `trainer.py` `/study` + gateway `/studies` surfaces, and the minimal UI study
  surface (optimize toggle + study/best-trial); confirm no torch-family movement and the GPU-lock hold
  time per trial is unchanged. (SC-074)

---

## Dependencies & Execution Order

- **T222 (pre-flight) gates everything** — never build HPO without confirming `optuna` doesn't disturb the
  frozen GPU stack and that 011's eval exposes a usable objective.
- **US1 (study runner, T223–T228)** is the spine and where the **sequential / VRAM-mutex** constraint
  lives; it leads.
- **US2 (spaces + objective, T229–T231)** needs the study runner to exist; **US3 (register + UI,
  T232–T235)** needs a scored best trial.
- **T236/T237 land last** (need every tier in place).

### Constitution gates (re-check each phase)
- Principle II untouched: trials **strictly sequential** (`n_jobs=1`), one model in VRAM ever; verify no
  two-resident-models window.
- Principle III: `optuna` is server-less, in-memory/SQLite study store, **no daemon**; **Ray Tune
  rejected**; only the best trial registered (no registry/MinIO flood).
- Principle VI strengthened: every trial an MLflow child run; the winner a tagged, promotable version.
- No new runtime → **no amendment** (Optuna is a library, like the ephemeral Prefect).

## Implementation Strategy

1. **Pre-flight** (`optuna` clean + 011 objective callable) → **study runner** (sequential, parent/child
   runs, failed-trial handling). **Stop and validate** (SC-070).
2. **Search spaces + objective** wired to 011's eval → **stop and validate** (SC-071/SC-072).
3. **Best-trial register + UI surface** → eligible for 011's gate → **stop and validate** (SC-073).
4. Each phase re-runs the relevant 001–011 tests on the GPU machine; never regress the single-value
   `POST /runs` path; never move the frozen GPU stack.

## Out of Scope (recorded)
- **Parallel / distributed HPO** (multi-GPU, multi-worker, **Ray Tune**): the single-GPU lease forces
  sequential trials — rejected in plan.md → Complexity Tracking.
- **Replacing the single-value fine-tune path**: `POST /runs` stays; HPO is additive (FR-118).
- **A new objective metric**: the objective is 011's eval metric — no competing definition.
- **An always-on HPO scheduler / AutoML daemon**: a study runs to completion and exits (Principle III).
- **Tuning the frozen GPU/FT library versions**: 012 tunes *training hyperparameters*, not torch/
  transformers/peft pins.
- **Full live per-trial UI visualization**: a documented **fast-follow**; 012 ships only the minimal
  Runs-UI surface (optimize toggle + study status + best-trial display).
