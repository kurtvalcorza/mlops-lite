# Implementation Plan: Hyperparameter Optimization (Optuna + MLflow)

**Branch**: `012-hyperparameter-optimization` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/012-hyperparameter-optimization/spec.md` (HPO over the
fine-tune path, optimizing toward 011's eval metric)

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

## Summary

Add **hyperparameter optimization** to the fine-tune path using **Optuna** (pure-Python, server-less)
with **MLflow** parent/child-run tracking: (US1) an Optuna **study runner** that, per trial, samples a
hyperparameter set and runs the **existing** `finetune_flow` as a real training run — **strictly
sequentially on the single GPU** (one model in VRAM, Optuna `n_jobs=1`); (US2) **per-modality search
spaces** with the trial **objective wired to 011's eval metric**; (US3) the **best trial → registered**
MLflow model version (eligible for 011's gated promotion) and the study/trials **surfaced in the Runs
UI**. New dependency: `optuna` (light). **No new runtime, no new daemon, no Ray.** Phase-gated like
prior increments, validated against the full 001–011 suite, never regressing the single-value fine-tune
path or the VRAM mutex.

## Technical Context

**Language/Version**: Python 3.12 (gateway image, post-007), native WSL training venv (PEFT/LoRA via
ephemeral Prefect). No new language or runtime — `optuna` is a library, not a service.

**Primary Dependencies**: **NEW** `optuna` (latest stable; pure-Python, in-process; default in-memory /
local-SQLite study storage). Existing: `mlflow`/`mlflow-skinny` (3.x, post-007 — parent/child runs +
the registry), Prefect (ephemeral flow structure, unchanged), the **frozen** GPU/FT stack
(torch cu128 / transformers / peft / accelerate / datasets — 012 does **not** touch it; it tunes
*training hyperparameters*, not library versions). The **011 eval harness** is a hard dependency (the
objective).

**Optuna integration note**: Optuna is server-less by design — `optuna.create_study(...)` runs in the
study-runner process exactly as the ephemeral Prefect `@flow` does. Each trial = one `study.optimize`
step calling an objective that (a) invokes `finetune_flow` with sampled params, (b) runs 011's eval on
the result, (c) returns the scalar. MLflow integration is **manual + explicit** (parent `start_run` for
the study; nested `start_run(nested=True)` per trial) — same explicit-tracking discipline as 006/011,
not autolog. `study.optimize(objective, n_trials=N, n_jobs=1)` enforces sequential trials.

**Objective / 011 dependency**: the trial objective is **011's eval-harness scalar** for the trained
candidate, optimized in the metric's declared direction. No divergent metric. If 011's eval errors for a
candidate, the trial is marked failed (Optuna `TrialPruned`/exception), not scored as a valid-but-bad
trial.

**Storage**: Optuna study storage defaults to **in-memory** (or a tiny local SQLite file under the
training workdir) — local, ephemeral, well within Principle III; no Postgres/MinIO footprint beyond what
the trials' MLflow runs already use. The best trial's GGUF/model artifact lands in MinIO via the
existing `register_version` path.

**Target Platform**: Win11 + WSL2 + Rancher Desktop. Training (and therefore every trial) runs **native
in WSL** on the single GPU (hybrid GPU, constitution v1.2.0). The study runner lives on the training
side (native trainer / flow), the gateway proxies study launch + status like it proxies `POST /runs`.

**Project Type**: additive feature over the fine-tune path (002/003/.../011). Touches the training side
(`training/flows/` — a new HPO study module wrapping `finetune_flow`; `training/trainer.py` — a study
launch/status surface alongside `/train`), the gateway (`gateway/app/routers/runs.py` — a study
launch/status route alongside `/runs`), and the UI (`ui/` — surface studies + trials in the Runs tab).
**No change to the frozen GPU/FT stack; no change to the single-value `POST /runs` path.**

**Performance Goals**: none beyond "don't regress single-fine-tune latency or GPU-lock hold time". HPO
wall-clock is **inherently** `n_trials × per-train-time` (sequential on one GPU) — a property to surface,
not optimize away.

**Constraints**: one model in VRAM at any instant (trials sequential, `n_jobs=1`); objective = 011's eval
metric; Optuna server-less (no daemon, no Ray); additive (single-value `POST /runs` unchanged); idle
footprint unchanged (Principle III).

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Optuna runs in-process on the host; nothing leaves the machine | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | Trials **strictly sequential** (`n_jobs=1`); at most one model in VRAM ever; each trial frees VRAM before the next | ✅ unchanged |
| III. Lightweight Footprint | `optuna` is pure-Python, **server-less**, in-memory/SQLite study store; **no new daemon**; **Ray Tune explicitly rejected** as too heavy | ✅ |
| IV. Full Lifecycle Coverage | HPO **enriches** the training stage (better hyperparameters → better registered model); adds no stage, drops none | ✅ N/A |
| V. OSS & Swappable | Optuna is mainstream OSS behind the trainer interface; the objective is 011's eval, swappable; MLflow stays the tracker/registry | ✅ |
| VI. Reproducibility & Observability | Every trial is an MLflow child run with sampled params + objective logged; the study is a parent run; the winner is a registered, tagged version | ✅ strengthened |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1 study runner, US2 spaces+objective, US3 register+UI), each validated on the GPU | ✅ |
| Workflow: "no new runtime without amendment" | None introduced — `optuna` is a library (no server/daemon), like the ephemeral Prefect already in the flow | ✅ no amendment |

**No amendment required.** 012 adds one light Python library and **no runtime/daemon**; the single-GPU
sequential constraint *upholds* Principle II rather than straining it, and per-trial MLflow tracking
advances Principle VI. Ray Tune (which would add a scheduler/cluster) is rejected (Complexity Tracking).
Clean gate-check, mirroring 006/011's library-only, no-amendment posture.

## Project Structure

### Source Code (delta over 011)

```text
mlops-lite/
├── training/
│   ├── flows/
│   │   ├── finetune.py            # REUSED unchanged — a trial = one finetune_flow(...) invocation
│   │   └── hpo.py                 # NEW: Optuna study runner — sample → finetune_flow → 011 eval →
│   │                              #      objective; parent (study) + nested child runs; n_jobs=1;
│   │                              #      per-modality search spaces; best-trial → register_version
│   ├── search_spaces.py          # NEW: per-modality search-space declarations (LLM-LoRA vs vision)
│   └── trainer.py                 # MODIFIED: add POST /study + GET /study/{id} (study launch/status)
│                                  #           alongside the existing /train; one study at a time on the lease
├── gateway/app/routers/
│   └── runs.py                    # MODIFIED: proxy POST /studies + GET /studies/{id} alongside /runs
├── training/requirements.txt      # MODIFIED: add `optuna` (light); GPU/FT stack LEFT FROZEN
├── ui/                            # MODIFIED: Runs tab surfaces studies + trials + highlighted best
└── tests/                        # NEW: test_hpo_study (sequential trials, child runs),
                                   #      test_hpo_objective (objective == 011 eval), test_hpo_register
                                   #      (best-trial → one version), + no-regression on /runs
```

**Structure Decision**: the HPO study runner lives **on the training side** (`flows/hpo.py`) so it reuses
`finetune_flow` and 011's eval in-process on the GPU host; the trainer daemon gains a `/study` surface
mirroring `/train`, and the gateway proxies it mirroring `/runs`. The single-value fine-tune path and the
frozen GPU/FT stack are untouched — HPO is a wrapper, not a rewrite.

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm `optuna` installs clean in the training venv without perturbing the
  frozen torch/transformers/peft pins (`pip install optuna` is pure-Python; verify no torch-family
  movement). Confirm 011's eval harness exposes a callable scalar objective + its optimize direction.
- **Phase 1 — Study runner (US1)**: `flows/hpo.py` Optuna study, `n_jobs=1`, per-trial `finetune_flow`
  invocation, MLflow parent + nested child runs, failed-trial handling (no partial version, free GPU,
  continue); `trainer.py` `/study` launch/status (one study at a time, respects serving-resident 409);
  gateway `/studies` proxy. Exit: SC-070.
- **Phase 2 — Search spaces + objective (US2)**: `search_spaces.py` per-modality spaces (LLM-LoRA /
  vision); wire the trial objective to **011's eval metric** + optimize direction; failed-eval → failed
  trial. Exit: SC-071 + SC-072.
- **Phase 3 — Best-trial register + UI (US3)**: best trial → single registered MLflow version (tagged
  study id + winning params + winning metric) via the existing `register_version`, eligible for 011's
  gated promotion; surface study/trials/best in the Runs UI. Exit: SC-073.
- **Phase 4 — Cross-cutting regression**: full 001–011 sweep green; single-value `POST /runs` unchanged;
  VRAM mutex + one-run/one-study-at-a-time intact; idle footprint unchanged (no resident Optuna process).
  Exit: SC-074.

Cross-cutting: the four grill items are **resolved** (spec.md → Grilled decisions): per-modality **default
ranges, configurable**; small default **`n_trials` (~15) + optional `timeout`**; **full trials by default,
`MedianPruner` opt-in**; **gateway/CLI core + minimal Runs-UI** (optimize toggle + study/best-trial, full
live-trial UI = fast-follow). The search-space and pruning seams stay isolated so each lands in one place.

## Complexity Tracking

| Decision | Why Needed | Simpler / Heavier Alternative Rejected Because |
|---|---|---|
| **Optuna** (not Ray Tune) | Pure-Python, server-less, in-process — same spirit as the ephemeral Prefect; one light dep, no daemon, MLflow-integratable via parent/child runs | **Ray Tune rejected**: pulls in a Ray cluster/scheduler runtime — a heavy always-on component that breaches Principle III and would need a constitution amendment, for zero benefit on a single-GPU sequential host |
| Trials **strictly sequential** (`n_jobs=1`) | One-model-in-VRAM (Principle II): each trial is a lease tenant; two resident models is a violation | Parallel/distributed trials would need >1 GPU or VRAM-sharing — impossible on this host and a direct Principle II violation |
| A trial = the **existing `finetune_flow`** verbatim | Reuse the validated train→GGUF→register path + its VRAM discipline; HPO is a wrapper | Reimplementing training inside the study runner would duplicate (and risk diverging from) the trainer and its VRAM-free discipline |
| Objective = **011's eval metric** (no new metric) | One definition of "good", already gated for promotion; the HPO winner flows into 011's gate | A bespoke HPO objective would diverge from the promotion gate — a study could "win" on a metric the gate doesn't trust |
| **Only the best trial** is registered | Avoids polluting the registry with N versions per study; the winner is the promotable artifact | Registering every trial floods the registry/MinIO with throwaway versions, breaching Principle III disk-frugality |
| Optuna study store **in-memory / local SQLite** | Tiny, local, ephemeral — within Principle III; no Postgres/MinIO footprint | A persistent distributed study store (DB-backed RDBStorage on a server) is unneeded for single-host sequential runs and adds footprint |
| **Full trials by default**; `MedianPruner` opt-in | The objective is 011's eval metric (computed post-train); the only cheap mid-train signal is training loss — an imperfect proxy, so pruning on it risks killing a trial that would have won. Default-off avoids mis-prune; `MedianPruner` stays available as opt-in config (needs an optional `trial.report()`/`should_prune()` hook in the trainer) for users who accept the proxy | **Pruning-by-default rejected**: would prune against a loss proxy that doesn't match the eval-metric objective → mis-prune risk on a single-GPU host where each trial is already expensive. **A bespoke mid-train eval-on-the-objective hook rejected**: re-running 011's eval mid-training per trial is costly and re-enters the VRAM lease — net-negative vs just finishing the trial |
