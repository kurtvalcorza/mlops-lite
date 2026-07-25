# Implementation Plan: Close Lifecycle Gaps

**Branch**: `025-close-lifecycle-gaps` (working branch `claude/codebase-architecture-improvements-udog5o`) | **Date**: 2026-07-22 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/025-close-lifecycle-gaps/spec.md`

## Summary

Close the real gaps found in the full-loop review. Unlike 024 (behavior-preserving), 025 **changes behavior and adds capability**, so each change is explicit and constitution-checked. Committed core: **US1 batch correctness** (score the requested version under the lease, restore the prior target, and hold a batch-wide exclusion so online `/infer` never sees the batch's version; fix the broken tabular batch payload and the GPU-alias protection gap; ASR is already rejected at submission, so any ASR batch path is optional net-new, not a fix) and **US2 tabular full modality** (fine-tune flow + AUC fixture/gate + quality). Lower-priority, phased: **US3–US6** surface previously-parked operator/data features (dataset byte-download, streamed-prediction logging, live HPO progress, shadow-replay UI). Approach: extend existing seams (no new engines), keep tabular CPU/off-lease, add no heavy deps, and validate the GPU-touching legs on hardware.

## Technical Context

**Language/Version**: Python 3.11 (gateway/agent/training/platformlib); TypeScript/Next.js for `ui/` (console-only, per constitution).

**Primary Dependencies**: unchanged where possible. Tabular training reuses the LightGBM already present for tabular serving; AUC stays pure-Python. **No heavy dependency added** (FR-360). HPO progress must avoid an external Optuna dashboard service.

**Storage**: Postgres `gateway` DB + Garage S3, both present. No schema change is anticipated; tabular quality reuses existing `predictions`/`labels`/`capture` tables and streamed capture reuses the existing capture path. Any genuinely-needed change lands as a NEW numbered `platformlib/migrations/*.sql` (FR-359).

**Testing**: pytest offline suite (web-free where logic is web-free) + live/HW-gated legs via `conftest` guards. Batch load-under-lease and any GPU path are `[HW]` SCs (constitution gate zero).

**Target Platform**: single machine (Compose + native WSL agent/UI); GPU only where serving/loading is involved (batch US1). Tabular is CPU.

**Project Type**: capability + fix increment over the existing web-service + native-agent + shared-lib + Next.js-console monorepo.

**Performance Goals**: no regression; batch version-assertion must not add a second load per record (load once under the lease, then score the batch).

**Constraints**: one GPU tenant (Principle II) — batch loads go through admission, jobs non-preemptable; dependency-light (III); console-only Node (workflow amendment); fail-open capture off the response path (streamed logging).

**Scale/Scope**: US1 ≈ **medium, not the 2-liner it first looked** (Codex rounds 4-5): an agent-side version load/assert seam + a `finally` restore (load-failure-safe) + a batch-wide online-inference exclusion + GPU-alias protection normalization + the broken tabular batch-payload fix — the single-GPU engine sharing is what turns "score the right version" into real concurrency work. US2 ≈ a modality slice (new `tabular_finetune` flow + scorer + fixture + quality wiring) **plus** warm-child version-invalidation on promote and policy/retrain validator decoupling. US3–US6 ≈ mostly UI + thin BFF/stream surfaces over existing backends (each new endpoint/SSE-shape change lands its own contract update per FR-359).

## Constitution Check

*GATE: re-checked after design.*

| Principle | Verdict | Notes |
|---|---|---|
| I. Local-First, Single-Machine | ✅ Pass | No cloud/cluster; all changes local. |
| II. Single-GPU, On-Demand (NON-NEGOTIABLE) | ✅ Pass (requires HW validation) | US1 batch load goes through admission; jobs stay non-preemptable. The load-under-lease leg MUST be validated on the GPU box (SC-175). Tabular is CPU/off-lease. |
| III. Lightweight Footprint | ✅ Pass | No heavy dep added; tabular reuses LightGBM; HPO progress avoids an external dashboard service (FR-360). |
| IV. Full Lifecycle Coverage | ✅ Pass / advances | US2 makes tabular a *full* modality (train→gate→serve→monitor), extending coverage rather than dropping a stage. |
| V. Open-Source & Swappable | ✅ Pass | Tabular metric/scorer behind the existing metric interface. |
| VI. Reproducibility & Observability | ✅ Pass | Tabular versions tracked with logged metric; streamed predictions become observable (US4). |
| VII. Incremental, Phase-Gated | ✅ Pass | US1/US2 committed core; US3–US6 phase into 026+ if larger than a slice — no big-bang. |

**Result**: no violations. The only gate that cannot be satisfied offline is Principle VII's on-hardware verification for US1's load-under-lease leg (and any GPU SC) — flagged as `[HW]` tasks, not skipped.

## Project Structure

### Documentation (this feature)

```text
specs/025-close-lifecycle-gaps/
├── plan.md
├── spec.md
├── tasks.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
training/flows/
├── batch_infer.py            # US1: load/assert requested version under lease, restore prior target in finally; ASR already rejected at submission (optional net-new path only)
└── tabular_finetune.py       # US2: NEW — tabular fine-tune flow (mirrors vision_finetune.py)

training/scoring/
└── tabular.py                # US2: NEW — tabular prediction factory (predict_fn); NOT a new AUC impl

benchmarks/tabular/
└── auc_smoke.jsonl           # US2: NEW — committed held-out tabular eval fixture

hostagent/jobs.py             # US1: keep GPU_BATCH_MODALITIES consistent with the flow's real paths
gateway/app/evaluation.py     # US2: tabular AUC promoted from stub to committed metric + live path
gateway/app/quality.py        # US2/US4: tabular quality wiring; streamed-prediction capture
gateway/app/routers/stream.py # US4: capture streamed predictions fail-open, off the response path
gateway/app/routers/datasets.py + ui/app/data/            # US3: dataset byte-download via BFF
ui/app/training/ (+ a progress stream)                    # US5: live HPO trial progress
ui/app/models/ (+ existing shadow endpoints)              # US6: shadow-replay dispatch/verdict UI

tests/
├── test_batch_version_assert.py   # US1 (offline ordering + injected predict_fn)
├── test_tabular_finetune.py       # US2 (seams; full run CPU/live-gated)
├── test_tabular_eval.py           # US2 (prediction factory + existing auc metric + gate, web-free)
├── test_stream_capture.py         # US4 (fail-open capture, web-free seam)
└── test_ui_*.py                   # US3/US5/US6 console surfaces
```

**Structure Decision**: extend existing homes (`training/flows`, `training/scoring`, `benchmarks/`, `gateway/app`, `ui/app`); no new top-level package. Tabular mirrors the proven `vision_finetune.py` shape so it inherits the modality-flow contract (dispatch, subprocess isolation, register-with-metric, failure cleanup).

## Design Phases

### Phase 0 — Research
Key decisions (settled in research D1/D4, not open): (a) US1 — **full load-under-lease + batch-wide online-inference exclusion** is the chosen scope (NOT assert-and-refuse); refuse cleanly if a job holds the GPU (never preempt); the `finally` restores by **re-reading the latest desired target** (a promote may land mid-batch), not by blindly restoring the captured snapshot; and the batch's OWN rows must bypass the exclusion (marker/token or agent-internal seam). ASR is already rejected at submission (`BATCH_MODALITIES` excludes it) — no fix is needed; add an ASR batch path only if batched transcription is genuinely wanted (net-new: admit `asr` *and* provide the path together), otherwise it is a no-op. (b) US2 — tabular training library = the LightGBM already shipped for serving; AUC stays pure-Python; the tabular quality label source is per-request (no modality-wide documented exclusion — FR-353/SC-178); a promoted version reloads on a warm child (FR-351). (c) US4 — reuse the existing fail-open capture seam so the streamed path matches the non-streamed contract exactly. (d) US5 — an in-process progress stream, no external Optuna dashboard.

### Phase 1 — Contracts and models
- No new persisted entities expected; if tabular quality needs a column, it is a NEW numbered migration (FR-359).
- Contract updates only where an endpoint is added (dataset download, HPO progress stream, shadow-replay is already contracted).

### Phase 2 — Tasks
See [tasks.md](./tasks.md): US1 and US2 first (committed core), then US3–US6 as independently-shippable slices, each with tests and (for GPU legs) an on-hardware validation task.

## Complexity Tracking

No constitution violations — section intentionally empty. The only residual risk is scope sprawl across US3–US6, mitigated by the phase-gating in Assumptions (spin off to 026+ rather than bloat a single PR).
