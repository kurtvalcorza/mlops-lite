# Implementation Plan: Shadow-Replay Champion-Challenger

**Branch**: `016-shadow-replay` | **Date**: 2026-06-30 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/016-shadow-replay/spec.md` (DRAFT — GRILLED).

## Summary

Add **production-traffic** champion-challenger (the deferred half of 011). First **extend 013's
prediction logging** to capture *recoverable* inputs (prompt/image/audio) under a **sampled + capped +
TTL** policy behind the `QUALITY_CAPTURE_IO` opt-in. Then a new on-demand endpoint `POST
/models/{name}/shadow-replay` dispatches an **async trainer-side job** that loads the challenger under the
single GPU lease, scores it over the **captured ∩ labeled** replay window by **reusing 015's in-process
per-modality scorers** (replay corpus as the row source), reads the **champion's already-logged quality**
on the same window, and persists an **advisory** verdict. The hard promotion gate (011/015) is untouched.

## Technical Context

**Language/Version**: Python 3.12 (gateway + native trainer venv). No new runtime.

**Primary Dependencies**: Existing only — 013's `gateway/app/quality.py` logging + MinIO `results` bucket;
**015's** `training/scoring/` in-process per-modality scorers; 011's pure-Python metrics. **No new
dependency** (Principle III).

**Storage**: MinIO `results` bucket — a new bounded `inputs/` capture (sampled+capped+TTL) alongside the
existing `predictions/`, `labels/`, `quality/` prefixes; verdicts under a `shadow/` prefix (and/or MLflow).

**Testing**: `pytest` (offline: capture policy + window intersection + verdict math with injected scorers/
fakes) + on-hardware (a labeled+captured window → a real shadow-replay verdict; `nvidia-smi` one-model).

**Target Platform**: Win11 + WSL2 + NVIDIA (hybrid-GPU). Capture runs in the **gateway** (serving path,
fire-and-forget); the replay scoring runs in the **native trainer** (reusing 015's scorers + the lease).

**Project Type**: Local MLOps platform. Change spans a 013-logging extension (gateway), a trainer-side
shadow-replay job (reusing 015), and a small gateway endpoint + verdict storage.

**Performance Goals**: Capture is fire-and-forget + bounded (no serving impact, FR-119). Replay scoring is
one challenger pass over a small window (≤ `WINDOW_N`, default 100 labeled pairs).

**Constraints**: **One model in VRAM (Principle II)** — challenger loaded sequentially under the lease.
Bounded capture (Principle III). Capture opt-in (privacy). Frozen GPU stack untouched. Public repo.

**Scale/Scope**: Replay window ≤ ~100 labeled+captured pairs; one challenger load per shadow-replay job.

**Dependency note**: **Build after 015 merges** (reuses `training/scoring/`). 016's own logging extension
(US1) can be developed in parallel, but US2 needs 015's scorers.

## Constitution Check

*GATE: must pass before Phase 0. Re-checked after design.* Constitution **v1.4.0**.

| Principle | Assessment |
|---|---|
| **I. Local-First** | ✅ Capture + replay are local (MinIO + native trainer); no cloud. |
| **II. Single-GPU On-Demand (NON-NEGOTIABLE)** | ✅ Challenger loaded **sequentially under the single lease**; champion not re-run; never co-resident. No new lease/admission path. |
| **III. Lightweight Footprint** | ✅ **No new dependency**. Capture is **sampled + capped + TTL** (bounded image/audio storage); reuses 013 logging, 015 scorers, 011 metrics. No new idle service. |
| **IV. Full Lifecycle Coverage** | ✅ Strengthens the evaluate stage with a production-traffic signal; adds no stage. |
| **V. OSS & Swappable** | ✅ Reuses existing OSS components + the 015 `predict_fn` seam. |
| **VI. Reproducibility & Observability** | ✅ **Advances it** — evaluates on the real served distribution and records the verdict + provenance (window size, pair count). |
| **VII. Incremental, Phase-Gated** | ✅ One increment, verifiable on the reference hardware. |

**Verdict: PASS — no amendment.** The verdict is **advisory** (does not touch 011's single gate
choke-point); capture is bounded + opt-in. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/016-shadow-replay/
├── spec.md          # grilled spec
├── plan.md          # this file
├── research.md      # Phase 0 — the 5 grilled decisions + rationale
├── data-model.md    # Phase 1 — captured input / replay window / verdict
├── quickstart.md    # Phase 1 — on-hardware validation guide
├── contracts/
│   ├── shadow-replay-endpoint.md   # POST/GET /models/{name}/shadow-replay
│   └── capture-extension.md        # the 013 logging extension (sampled+capped+TTL)
└── tasks.md         # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
gateway/app/
├── quality.py                 # US1: extend log_prediction to capture a RECOVERABLE input
│                              #   (prompt/image/audio) under a sampled+capped+TTL policy behind
│                              #   QUALITY_CAPTURE_IO; new inputs/ prefix; pure policy fns + storage
├── routers/
│   ├── infer.py / stream.py / vision.py / transcribe.py   # pass the recoverable input to capture
│   └── models.py              # US2: POST /models/{name}/shadow-replay (dispatch) + GET (verdict)
└── shadow.py (NEW)            # gateway-side: resolve the replay window (captured ∩ labeled), read the
                               #   champion's logged quality, dispatch the trainer job, store/read verdict

training/
├── scoring/                   # REUSED from 015 — the in-process per-modality scorers (predict_fn)
└── flows/shadow_replay.py (NEW)  # trainer-side job: load challenger under the lease → score over the
                               #   replay corpus via 015's scorer → return per-metric value

tests/
├── test_capture_policy.py     # NEW — sampled+capped+TTL capture; opt-in off ⇒ nothing
├── test_shadow_window.py      # NEW — captured ∩ labeled intersection + insufficient-data (MIN_PAIRS)
└── test_shadow_verdict.py     # NEW — advisory verdict math (challenger-replay vs champion-logged)
```

**Structure Decision**: Single-project. US1 (capture) is a gateway-side extension of 013's `quality.py`;
US2 (replay) is a trainer-side job reusing 015's `training/scoring/` + the lease, fronted by a small
gateway dispatch/verdict surface. No new service or daemon.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.

## Phase 0 — Research (see research.md)

The 5 grilled decisions as Decision / Rationale / Alternatives. Remaining items to pin (non-blocking): the
capture storage layout (`inputs/` prefix vs extending the `predictions/` record), the sampling primitive
(rate vs ring-buffer cap vs both) + TTL prune (lazy-on-write vs sweep), and the verdict persistence
location (`results` `shadow/` prefix vs an MLflow run).

## Phase 1 — Design & Contracts

- **data-model.md**: Captured input, Replay window (captured ∩ labeled), Shadow-replay verdict.
- **contracts/**: `shadow-replay-endpoint.md` (the async `POST` + `GET` shape, advisory verdict body) and
  `capture-extension.md` (the 013 logging extension: config knobs, opt-in, bounding, recoverable-input
  shape per modality).
- **quickstart.md**: enable capture → serve a labeled window → shadow-replay a challenger → assert an
  advisory verdict + one-model-in-VRAM + the gate unchanged + insufficient-data path.

## Phase 2 — Tasks (/speckit-tasks)

Generated separately. Expected: (US1) the capture extension + per-modality recoverable-input wiring +
policy/tests; (US2) the trainer-side `shadow_replay.py` job reusing 015's scorers + the gateway
dispatch/verdict endpoint + champion-from-logs + verdict math; (US3) insufficient-data / no-corpus
handling; plus the no-regression sweep (001–015) and the dual-bot loop. **Sequenced after 015.**
