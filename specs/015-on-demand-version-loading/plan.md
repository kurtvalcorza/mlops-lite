# Implementation Plan: Score-at-Registration (closing SC-068)

**Branch**: `015-on-demand-version-loading` | **Date**: 2026-06-30 | **Spec**: [spec.md](spec.md)

**Status**: **BUILT (offline) — on-hardware SCs pending Kurt's GPU box.** See tasks.md.

**Input**: Feature specification from `specs/015-on-demand-version-loading/spec.md` (DRAFT — GRILLED).

## Summary

Close SC-068 by making every fine-tuned model version **born with its eval metric**: each fine-tune
(LLM/vision/embeddings/ASR) scores its own model on its modality's held-out benchmark **in-process at
registration**, within its existing GPU-lease hold, and logs the metric against the new version. The
gate, `compare`, quality, and the HPO objective then **read logged metrics** — no serving-daemon reload,
no per-version loading machinery. The gateway standalone `/evaluate`/`/compare` get a **guard** (clear
error instead of silently scoring the resident model). LLM/ASR score their **served artifact** (GGUF via
a transient llama.cpp / ggml via a transient whisper.cpp); vision/embeddings score the in-memory torch
model. 015 also ships the embeddings (recall@k) + ASR (WER) benchmark fixtures so all four modalities
score at registration. Trainer-side scoring is in-process, so the trainer→daemon-URL gap (finding #4)
disappears.

## Technical Context

**Language/Version**: Python 3.12 (gateway), Python 3.12 native trainer venv `~/mlops-train` (frozen
cu128 stack). No new runtime.

**Primary Dependencies**: Existing only — `transformers`/`peft`/`torch` (already loaded by the trainer),
the already-built **llama.cpp** (`~/llama.cpp`, llama-cli/llama-server) and **whisper.cpp**
(`~/whisper.cpp`) binaries, `sentence-transformers`, and 011's **pure-Python** metric functions in
`gateway/app/evaluation.py`. **No new dependency** (Principle III).

**Storage**: Existing — MLflow registry (version tags + run metrics via `evaluation._log_eval`), MinIO
(adapters/artifacts already produced by the fine-tune flows). New tiny held-out fixtures under
`benchmarks/embedding/` and `benchmarks/asr/` (content-hashed JSONL, like the LLM/vision ones).

**Testing**: `pytest` (offline, importlib + injected fakes for the scoring seams) + on-hardware
validation (a real fine-tune per modality scores at registration; a 2-trial HPO study logs distinct
objectives; `nvidia-smi` shows ≤ one model resident through train→score).

**Target Platform**: Windows 11 + WSL2 Ubuntu + NVIDIA (RTX 5070 Ti reference); hybrid-GPU (gateway in
Docker, trainer + serving daemons native in WSL).

**Project Type**: Local MLOps platform — change is concentrated in the **native trainer** (`training/`)
plus a small **gateway** guard (`gateway/app/`) and two benchmark fixtures.

**Performance Goals**: Scoring adds one held-out pass per fine-tune (tiny smoke benchmarks: ~10 rows).
For LLM/ASR it adds a transient llama.cpp/whisper.cpp load+score+free within the existing lease hold.

**Constraints**: **One model in VRAM at any instant (Principle II)** — the training model is freed before
a served-artifact scorer loads; scoring stays inside the fine-tune's single lease hold. Frozen GPU stack
untouched. No heavy deps. Public repo (no secrets).

**Scale/Scope**: 4 trainable modalities; ~10-row smoke benchmarks; one extra scoring pass per fine-tune /
per HPO trial.

## Constitution Check

*GATE: must pass before Phase 0. Re-checked after design.* Constitution **v1.4.0**.

| Principle | Assessment |
|---|---|
| **I. Local-First** | ✅ No cloud dependency added; scoring runs on the local trainer over local fixtures. |
| **II. Single-GPU On-Demand (NON-NEGOTIABLE)** | ✅ Scoring is **sequential within the fine-tune's existing lease hold** — the training model is freed before any served-artifact scorer loads; **never two models resident**. No new lease, no second admission path. (Grilled decision 7.) |
| **III. Lightweight Footprint** | ✅ **No new dependency**; reuses already-built llama.cpp/whisper.cpp + torch + 011's pure-Python metrics. Two tiny (~KB) benchmark fixtures. No new idle service. |
| **IV. Full Lifecycle Coverage** | ✅ Strengthens the existing evaluate/promote stage; adds no stage. |
| **V. OSS & Swappable** | ✅ Reuses existing OSS components; the eval `predict_fn` seam stays injectable/swappable. |
| **VI. Reproducibility & Observability** | ✅ **Advances it** — every version now logs its eval metric in MLflow at registration ("if it isn't tracked, it didn't happen"); scoring the *served artifact* makes the logged metric honest. |
| **VII. Incremental, Phase-Gated** | ✅ One increment, verifiable on the reference hardware. |

**Verdict: PASS — no amendment.** This does not change the one-model-in-VRAM rule; it makes the *scored*
model the *registered* one. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/015-on-demand-version-loading/
├── spec.md          # grilled spec
├── plan.md          # this file
├── research.md      # Phase 0 — the 7 grilled decisions + rationale
├── data-model.md    # Phase 1 — registration EvalResult + per-modality predict_fn seam
├── quickstart.md    # Phase 1 — on-hardware validation guide
└── tasks.md         # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
benchmarks/
├── embedding/recall_smoke.jsonl     # NEW — tiny held-out recall@k fixture (content-hashed)
└── asr/wer_smoke.jsonl              # NEW — tiny held-out WER fixture (content-hashed)

training/
├── flows/
│   ├── finetune.py      # LLM flow: after GGUF convert + register, score the served GGUF (transient
│   │                    #   llama.cpp) within the lease hold → log eval metric on the new version
│   ├── vision.py        # vision flow: score the in-memory torch model → log metric
│   ├── embeddings.py    # embeddings flow: score in-memory ST model (recall@k) → log metric
│   ├── asr.py           # ASR flow: score the served ggml (transient whisper.cpp, WER) → log metric
│   └── hpo.py           # objective = the trial's own registered metric (from the flow above)
├── scoring/             # NEW — in-process per-modality scorers (predict_fn impls) + a thin
│                        #   "score_and_log(version, modality)" the flows call before lease release
└── trainer.py           # ensures scoring runs inside the existing lease hold (no release between)

gateway/app/
├── evaluation.py        # gate/compare already read logged metrics (no change to math); add the
│                        #   /evaluate guard helper (requested != @serving & no logged metric → error)
└── routers/models.py    # /evaluate returns the clear error per FR-143

tests/
├── test_score_at_registration.py   # NEW — per-modality scorer logs a version metric (injected fakes)
├── test_eval_guard.py              # NEW — /evaluate guard: unscored non-serving version → error
└── (hpo / gate / eval-harness suites extended for the logged-metric objective)
```

**Structure Decision**: Single-project layout. The weight lands in `training/` (a new `scoring/` module
+ a `score_and_log` call wired into each fine-tune flow and the HPO trial), with a small gateway guard.
No new service or daemon; the serving daemons are **not** modified (the grill dropped per-version loading).

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.

## Phase 0 — Research (see research.md)

Records the 7 grilled decisions as Decision / Rationale / Alternatives. Remaining items to pin in
research.md (no blocking unknowns): the exact in-trainer invocation for the transient llama.cpp /
whisper.cpp scorers (llama-cli vs a short-lived llama-server; how to load base+LoRA-GGUF for scoring),
and the recall@k / WER fixture shapes.

## Phase 1 — Design & Contracts

- **data-model.md**: the registration-time `EvalResult` (reusing 011's tag schema) and the per-modality
  `predict_fn(rows, modality, version) -> preds` contract for the four in-trainer scorers.
- **contracts/**: the one external contract change — gateway `POST /models/{name}/evaluate` guard
  behavior (FR-143): same request shape, new clear-error response when the requested version is not
  `@serving` and has no logged metric. (compare/gate/quality contracts unchanged — they read logged
  metrics.)
- **quickstart.md**: bring-up + per-modality fine-tune → assert a logged metric on the new version; a
  2-trial HPO study → assert distinct objectives + no hostname error; `nvidia-smi` one-model-in-VRAM check.

## Phase 2 — Tasks (/speckit-tasks)

Generated separately. Expected shape: (US1) the `training/scoring/` module + per-modality scorers + the
in-lease `score_and_log` wiring into each flow; (US2) HPO objective reads the trial's logged metric +
`compare` reads logged metrics; (US3) the gateway `/evaluate` guard; plus the two benchmark fixtures,
tests, and the no-regression sweep (SC-093).
