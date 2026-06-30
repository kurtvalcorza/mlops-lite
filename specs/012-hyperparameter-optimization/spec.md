# Feature Specification: Hyperparameter Optimization (Optuna + MLflow)

**Feature Branch**: `012-hyperparameter-optimization`

**Created**: 2026-06-28

**Status**: **BUILT & MERGED (2026-06-29, PR #14)** — US1–US3. Optuna study runner
(`training/flows/hpo.py`) + per-modality search spaces, optimizing 011's eval metric; sequential
trials on the single GPU lease (`n_jobs=1`); best trial registered for 011's gate. New dep
`optuna==4.9.0`. See tasks.md status block for evidence. (Was DRAFT — GRILLED 2026-06-28, build-ready.)

**Input**: Roadmap follow-on to 011 (the evaluation harness). The fine-tune path (`POST /runs` →
native trainer → `finetune_flow`) currently takes hyperparameters (`lora_r`, `steps`, `seed`, …) as
**hand-picked single values** from the Runs launch form. 012 adds **hyperparameter optimization (HPO)**:
an **Optuna** study that searches a per-modality space, runs each trial as a real fine-tune training
run, and optimizes toward **011's evaluation metric**. Each trial is an MLflow (child) run; the best
trial is registered as an MLflow model version eligible for 011's gated promotion. Optuna is pure-Python
with **no server** (same spirit as the ephemeral Prefect already in the flow — Principle III), so 012
adds exactly **one light dependency (`optuna`)** and **no new runtime**.

> **Scope note**: 012 **wraps** the existing trainer; it does not replace it. A trial is the existing
> `finetune_flow` invoked with one sampled hyperparameter set. Because each trial is a training run and
> a training run is a GPU-lease tenant, **trials run strictly sequentially on the one GPU** (Principle
> II) — total wall-clock = `n_trials × per-train-time`. The objective is **011's eval metric** (hard
> dependency on 011). Requirement IDs continue the shared space (FR-111+, SC-070+, tasks T222+). **No
> constitution amendment** — `optuna` is a light Python library (no server, no daemon), HPO adds no
> lifecycle stage and no always-on process. See plan.md → Constitution Check.

> **Hard dependency (011)**: HPO is meaningless without an objective. The optimization target is the
> scalar metric produced by **011's eval harness** on each trained candidate. If 011 is not present/green
> on the target machine, 012 cannot be validated. The eval call is the trial's objective function.

> **GPU/sequential constraint (NON-NEGOTIABLE)**: every trial loads a model to fine-tune → it is a
> one-model-in-VRAM lease tenant. Trials therefore **cannot run in parallel** on this single-GPU host.
> Optuna's parallelism (n_jobs) is pinned to **1**; the study is a sequential loop, each trial fully
> releasing VRAM before the next starts (same `del`/`empty_cache` discipline as `train_lora`).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Optuna study runner wrapping the trainer (Priority: P1)

An operator launches an HPO **study** (not a single run). The study runner creates an Optuna study,
and for each trial samples a hyperparameter set, invokes the **existing** `finetune_flow` with those
values as **one training run**, and records the trial under an MLflow **parent run** (the study) with
each trial as a **child run**. Trials execute **sequentially on the single GPU**, each respecting the
one-model-in-VRAM lease — exactly as a normal fine-tune run does today.

**Why this priority**: This is the spine — a study that turns N hyperparameter sets into N real,
sequential, MLflow-tracked training runs on the lease. Everything else (search spaces, objective,
best-trial registration) hangs off it. It is also where the GPU-sequencing constraint lives, so it
leads.

**Independent Test**: Launch a study with `n_trials=3` on a small dataset; observe three child runs
appear under one parent run in MLflow, each with its sampled params logged; confirm the three trials
ran one-at-a-time (never two models resident; no GPU contention) and the study finishes with a recorded
best trial. The trainer's existing 409 (serving-resident) behavior still gates the first trial.

**Acceptance Scenarios**:

1. **Given** an HPO study request with `n_trials=N` and a search space, **When** the study runs, **Then**
   exactly N trials execute **sequentially** (never two concurrently), each a real `finetune_flow`
   invocation, and each appears as an MLflow **child run** under one **parent (study) run**.
2. **Given** the single-GPU lease, **When** a trial is mid-flight, **Then** no second trial (and no
   serving model) is resident in VRAM — the study respects the one-model-in-VRAM mutex exactly as a
   single fine-tune run does.
3. **Given** a trial that fails (e.g. OOM, bad GGUF convert), **When** the study continues, **Then** the
   failed trial is recorded (pruned/failed state), no partial model version is registered for it, and
   the remaining trials still run.

---

### User Story 2 — Per-modality search spaces + objective wired to the 011 eval metric (Priority: P1)

Each trial samples from a **per-modality search space** (LLM-LoRA vs vision differ) and is scored by
**011's evaluation metric** computed on the trained candidate. Each modality ships **sensible default
ranges** (configurable, user-overridable — mirrors 010/011): **LLM-LoRA** = `lora_r` ∈ {8,16,32,64},
`lora_alpha` (tied or {16,32,64}), `lr` ∈ loguniform(1e-5, 5e-4), `steps`/epochs small range; **vision**
= `lr` ∈ loguniform, `epochs` small range, `freeze-depth` categorical. Optuna maximizes (or minimizes,
per the metric's direction) that scalar; the per-trial objective value and the sampled params are logged
to the trial's MLflow child run.

**Why this priority**: A study with no meaningful objective is just random training. Wiring the
objective to 011's eval metric is what makes the search an *optimization*. The per-modality spaces keep
the search relevant (LoRA rank means nothing for a vision classifier). P1 alongside US1.

**Independent Test**: Run an LLM-LoRA study; confirm each trial samples within the declared LLM space
(`lora_r`, `lora_alpha`, `lr`, `steps`), that each trial's objective equals the value 011's eval harness
returns for that candidate, and that Optuna's recorded best trial is the one with the best eval metric.
Repeat for a vision study with the vision space (`lr`, `epochs`, `freeze-depth`) — confirm the LLM-only
knobs are absent from the sampled space.

**Acceptance Scenarios**:

1. **Given** an LLM-LoRA study, **When** trials are sampled, **Then** the sampled hyperparameters fall
   within the declared LLM-LoRA search space and only LLM-relevant knobs are searched.
2. **Given** a trained candidate per trial, **When** the objective is evaluated, **Then** the trial's
   objective value is **011's eval-harness output** for that candidate, logged to the trial's child run,
   and Optuna's `best_trial` corresponds to the best eval metric (respecting the metric's optimize
   direction).
3. **Given** a vision study, **When** trials are sampled, **Then** the vision search space
   (`lr`/`epochs`/`freeze-depth`) is used and the LLM-LoRA knobs are not.

---

### User Story 3 — Best trial → register + surface in the Runs UI (Priority: P2)

When the study finishes, its **best trial** is registered as a new MLflow **model version** (tagged with
the study id, the winning hyperparameters, and the winning eval metric) — making it eligible for **011's
gated promotion**. The study is **surfaced through a minimal Runs-UI surface** — an "optimize" toggle on
the Runs launch form plus a display of study status + best-trial (params + winning metric); the study
appears alongside ordinary runs. (Full live per-trial visualization is a fast-follow — see Non-Goals.)

**Why this priority**: Closes the loop — an HPO study should leave behind a registered, promotable model,
not just an Optuna log. UI surfacing makes the study legible to the operator. P2 because the core
optimization (US1+US2) delivers value via MLflow even before the UI/registration polish.

**Independent Test**: After a study completes, confirm exactly one MLflow model version is registered for
the best trial (no version for non-winning trials), tagged with study id + winning params + winning
metric, and that 011's gated promotion accepts it as a candidate. In the Runs UI, confirm the minimal HPO
surface renders (optimize toggle + study status + best-trial params/metric).

**Acceptance Scenarios**:

1. **Given** a completed study, **When** registration runs, **Then** exactly **one** model version (the
   best trial's) is registered, tagged with the study id, winning hyperparameters, and winning eval
   metric; losing trials register **no** version.
2. **Given** the registered best version, **When** 011's gated promotion is exercised, **Then** the
   version is a valid promotion candidate (the HPO winner flows into the existing eval-gated promotion).
3. **Given** a finished study, **When** the operator opens the Runs UI, **Then** the minimal HPO surface
   renders — the "optimize" toggle on the launch form and the study's status + best-trial (winning params
   + winning metric) — alongside ordinary runs. (Full live per-trial visualization is a fast-follow.)

---

### Edge Cases

- **Single-GPU serialization**: trials are strictly sequential (Optuna `n_jobs=1`); the study is a loop,
  not a fan-out. Total time = `n_trials × per-train-time` — surfaced to the operator so an N-trial study
  isn't mistaken for a single run (FR-112).
- **Serving model resident at launch**: the first trial inherits the trainer's existing 409 (one model in
  VRAM); the study launcher surfaces "GPU busy" rather than silently queueing forever (FR-111).
- **Trial failure / OOM**: a failed trial is recorded as failed/pruned, registers **no** partial version,
  frees the GPU (`empty_cache`), and the study proceeds with the next trial (FR-113).
- **Pruning vs full trials**: **full trials by default** — every trial runs to completion, so there is no
  mis-prune risk against 011's eval-metric objective (the only cheap mid-train signal is training loss, an
  imperfect proxy). Optuna's `MedianPruner` is exposed as **opt-in config** (requires the trainer to report
  an intermediate signal via `trial.report()`/`should_prune()`); off by default (FR-113, rationale in
  plan.md → Complexity Tracking).
- **Objective unavailable**: if 011's eval harness errors for a candidate, that trial's objective is
  treated as failed (not silently scored 0), so a broken eval doesn't masquerade as a bad-but-valid trial
  (FR-115).
- **n_trials budget vs GPU wall-clock**: a **small default `n_trials` (~15)** plus an optional wall-clock
  `timeout` cap (both configurable) respects that each trial is a full train; total wall-clock ≈
  `n_trials × per-train-time` is surfaced to the operator (FR-112).
- **No regression**: the existing single-value `POST /runs` fine-tune path is unchanged — HPO is additive,
  not a replacement (SC-074).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-111**: The platform MUST provide an **HPO study runner** that wraps the existing fine-tune path:
  given a study request (`dataset_name`/`dataset_version`, `output_name`, modality, `n_trials`, search
  space), it creates an **Optuna** study and, per trial, samples a hyperparameter set and invokes the
  **existing** `finetune_flow` (US1's training run) — **reusing** the trainer, not forking it. Optuna
  runs **server-less** (in-process, like the ephemeral Prefect), adding only the `optuna` dependency.
  The study launch MUST respect the trainer's one-model-in-VRAM 409 (serving-resident) gate.
- **FR-112**: Trials MUST execute **strictly sequentially on the single GPU** (Optuna `n_jobs=1`); at
  most ONE model is resident in VRAM at any instant (Principle II). Each trial MUST fully release VRAM
  (`del`/`torch.cuda.empty_cache()`, as `train_lora` does) before the next trial starts. The study MUST
  surface that total wall-clock ≈ `n_trials × per-train-time` (an N-trial study is N sequential trains).
  The trial budget MUST default to a **small `n_trials` (~15)** with an optional wall-clock `timeout` cap;
  **both `n_trials` and `timeout` are configurable** per study (each trial is a full train).
- **FR-113**: Each trial MUST be tracked in MLflow as a **child run** under a single **parent (study)
  run**, with the sampled hyperparameters and the trial's objective value logged. A **failed** trial
  MUST be recorded (failed/pruned), MUST register **no** partial model version, MUST free the GPU, and
  MUST NOT abort the study — remaining trials still run. By **default every trial runs to completion**
  (no pruning — the only cheap mid-train signal is training loss, an imperfect proxy for 011's eval
  metric). Optuna's **`MedianPruner` MUST be available as opt-in config** (off by default); when enabled
  it requires the trainer to report an intermediate signal via `trial.report()`/`should_prune()` (an
  **optional** hook, not the default path).
- **FR-114**: The trial **objective** MUST be the scalar produced by **011's evaluation harness** on the
  trial's trained candidate (the optimization target). The study MUST optimize in the metric's declared
  direction (maximize accuracy/score, minimize loss/error). The objective wiring is a **hard dependency
  on 011** — no separate, divergent metric definition.
- **FR-115**: Per-modality **search spaces** MUST be declared with **sensible default ranges** so each
  modality searches only relevant knobs, and the defaults MUST be **configurable / user-overridable**
  (mirrors 010/011; defaults are not pinned immutable) — **LLM-LoRA**: `lora_r` ∈ {8,16,32,64},
  `lora_alpha` (tied or {16,32,64}), `lr` ∈ loguniform(1e-5, 5e-4), `steps`/epochs small range;
  **vision**: `lr` ∈ loguniform, `epochs` small range, `freeze-depth` categorical. ASR/embeddings spaces
  are carried as **guidance stubs**, implemented when those fine-tune paths land in 010. If 011's eval
  errors for a candidate, that trial's objective MUST be treated as **failed** (not silently scored as
  worst-valid).
- **FR-116**: When the study completes, its **best trial** MUST be registered as a new MLflow **model
  version**, tagged with the study id, the winning hyperparameters, and the winning eval metric. Only the
  best trial is registered (losing trials register no version). The registered version MUST be a valid
  candidate for **011's gated promotion** (it flows into the existing eval-gated promotion path, not a
  parallel one).
- **FR-117**: HPO's **core** is the gateway/CLI study runner + MLflow parent/child runs. On top of that,
  a **minimal Runs-UI surface** MUST be added: an **"optimize" toggle** on the Runs launch form plus a
  display of **study status + best-trial** (winning params + winning metric). The study appears alongside
  ordinary fine-tune runs through the same gateway Runs surface the operator already uses. **Full live
  per-trial visualization is a documented fast-follow** (Non-Goals), not part of 012.
- **FR-118**: HPO MUST be **additive** — the existing single-value `POST /runs` fine-tune path, the
  trainer daemon's one-run-at-a-time + VRAM mutex, and all 001–011 behavior MUST be unchanged. The new
  `optuna` dependency MUST stay within the lightweight footprint (Principle III): pure-Python, no server,
  no daemon, no Ray.

### Key Entities *(include if feature involves data)*

- **Study**: an Optuna study = one HPO campaign over a `(dataset, modality, search space, n_trials)`. Maps
  to a single MLflow **parent run**; owns the sequence of trials and the recorded best.
- **Trial**: one sampled hyperparameter set = one real fine-tune training run = one GPU-lease tenant =
  one MLflow **child run**, scored by 011's eval metric (the objective).
- **SearchSpace**: the per-modality set of tunable knobs + ranges (LLM-LoRA vs vision) Optuna samples from.
- **BestTrial**: the trial maximizing/minimizing the objective; the only trial promoted to a registered
  MLflow model version (eligible for 011's gated promotion).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-070**: A study with `n_trials=N` produces exactly **N** real fine-tune trials, run **sequentially**
  on the single GPU (never two models resident), each an MLflow **child run** under one parent (study) run.
- **SC-071**: Each trial's objective equals **011's eval-harness output** for that candidate; Optuna's
  recorded `best_trial` is the one with the best eval metric (in the metric's optimize direction).
- **SC-072**: Per-modality search spaces are honored — an LLM-LoRA study searches `lora_r`/`lora_alpha`/
  `lr`/`steps`; a vision study searches `lr`/`epochs`/`freeze-depth`; neither searches the other's knobs.
- **SC-073**: On study completion, exactly **one** model version (the best trial) is registered, tagged
  with study id + winning params + winning metric, and is accepted as a candidate by 011's gated
  promotion; the minimal Runs-UI surface (optimize toggle + study status + best-trial) renders.
- **SC-074**: No regression — the existing single-value `POST /runs` fine-tune path, the trainer's VRAM
  mutex / one-run-at-a-time, and the full 001–011 suite behave unchanged; idle footprint stays within
  Principle III (no new resident process from `optuna`).

## Assumptions

- **011 is the objective** — 011's eval harness exists and emits a scalar metric with a known optimize
  direction; HPO simply maximizes/minimizes it. 012 does not redefine "good".
- **One GPU ⇒ sequential trials** — there is exactly one model-in-VRAM lease, so a study is an inherently
  sequential loop. Optuna's distributed/parallel features are out of scope on this host (n_jobs=1).
- **Optuna is light and server-less** — like the ephemeral Prefect already in the flow, Optuna runs
  in-process with no always-on server; its study storage defaults to in-memory/SQLite (local, tiny), well
  within Principle III. No new daemon, no Ray, no cluster.
- **Trials reuse the trainer verbatim** — a trial is the existing `finetune_flow` with sampled params; the
  one-model-in-VRAM discipline, the GGUF convert, and the MLflow logging are reused, not reimplemented.
- **Best-trial registration feeds the existing gate** — the HPO winner is registered like any fine-tune
  output and promoted through **011's** eval-gated promotion, not a parallel promotion path.

## Non-Goals

- **Parallel / distributed HPO** — no multi-GPU, no multi-worker Optuna, no Ray Tune; the single-GPU lease
  forces sequential trials (rejected alternatives recorded in plan.md → Complexity Tracking).
- **Replacing the single-value fine-tune path** — `POST /runs` with hand-picked hyperparameters stays;
  HPO is an additional entry point, not a replacement (FR-118).
- **Tuning the GPU/FT stack itself** — 012 searches *training* hyperparameters (LoRA rank, lr, steps,
  epochs…), not the frozen torch/transformers/peft library versions.
- **A new metric** — the objective is 011's eval metric; 012 introduces no competing definition of model
  quality.
- **An always-on HPO scheduler / AutoML service** — no resident HPO daemon; a study runs to completion and
  exits (Principle III).
- **Full live per-trial UI visualization** — a documented **fast-follow**. 012 ships the minimal Runs-UI
  surface only (optimize toggle + study status + best-trial display); live trial-by-trial charts come later.

## Grilled decisions (2026-06-28)

All four "open for the grill" items are resolved; firm decisions (Optuna over Ray Tune, sequential
`n_jobs=1`, trial = existing `finetune_flow`, objective = 011's eval metric, best-trial → one registered
version, each trial = MLflow child run, additive) stand unchanged. No constitution amendment.

1. **Search spaces = sensible default ranges per modality, configurable** (mirrors 010/011; not pinned
   immutable). **LLM-LoRA** = `lora_r` ∈ {8,16,32,64}, `lora_alpha` (tied or {16,32,64}), `lr` ∈
   loguniform(1e-5, 5e-4), `steps`/epochs small range; **vision** = `lr` ∈ loguniform, `epochs` small
   range, `freeze-depth` categorical; ASR/embeddings carried as guidance stubs (land with 010). (FR-115,
   US2)
2. **Trial budget = small default `n_trials` (~15) + optional wall-clock `timeout`, both configurable.**
   Each trial is a full sequential fine-tune; total wall-clock ≈ `n_trials × per-train-time`. (FR-112)
3. **Pruning = full trials by default; pruning opt-in.** Every trial runs to completion (no mis-prune
   risk vs 011's eval-metric objective — training loss is the only cheap mid-train signal, an imperfect
   proxy). Optuna **`MedianPruner`** is exposed as opt-in config (requires the trainer to report via
   `trial.report()`/`should_prune()` — an optional hook); off by default. Rationale in plan.md →
   Complexity Tracking. (FR-113)
4. **HPO exposure = gateway/CLI core + minimal Runs-UI surface; full live-trial UI = fast-follow.** Core =
   study runner + MLflow parent/child runs. Minimal UI = "optimize" toggle on the Runs launch form +
   study status + best-trial (params + winning metric). Full live per-trial visualization is a documented
   fast-follow (Non-Goals). (FR-117, US3)
