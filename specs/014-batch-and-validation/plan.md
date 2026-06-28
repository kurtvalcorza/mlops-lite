# Implementation Plan: Batch Inference & Data-Validation Gates

**Branch**: `014-batch-and-validation` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/014-batch-and-validation/spec.md` (roadmap-planned lifecycle-
edge completion: offline batch inference + lightweight data-validation gates)

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

> **Grilled decisions (2026-06-28)** — the three open grill items are resolved:
> - **(A) Batch mechanism = ephemeral Prefect flow on the native daemon**, launched via the gateway
>   (`POST /batch` → native flow), consistent with training; acquire-once/release-at-end GPU lease, rows
>   scored through the serving path, content-addressed results to MinIO; CPU/tabular off-lease. The Docker
>   gateway has no GPU, so a gateway-endpoint+worker would proxy every row and could not hold the lease —
>   the native flow is correct + consistent. *(FR-128)*
> - **(B) Validation = sensible defaults, configurable; hard-gate critical / warn soft.** Default
>   hard-gate-for-training: required-columns/schema + min-row-count; default warn (configurable
>   thresholds): null-rate, value-range, label-balance; register-time advisory. All rules/thresholds/
>   dispositions configurable. *(FR-131/FR-132/FR-135)*
> - **(C) Batch UX = minimal Runs surface + record-and-continue.** Status/results in the existing Runs tab
>   (no new Batch tab); per-row failures recorded and the batch continues, bounded by a configurable abort
>   threshold. *(FR-134/FR-129)*

## Summary

Complete the two open lifecycle edges left by online-only inference and ungated training: (US1) **offline
batch inference** — submit a registered MinIO dataset version + a target model, score every record as a
job, write a content-addressed result artifact back to MinIO, with GPU-backed models going through the
existing single-VRAM lease and CPU models scoring off-lease; (US2) **lightweight, hand-rolled data
validation** — schema/null/range/label-balance/row-count checks producing a structured report, **hard-
gating** the training flow before the LoRA loop (required-columns/schema + min-row-count gate; null-rate/
range/label-balance warn; all configurable); (US3) **wire-in** — surface the validation report in the
Datasets surface and a batch launcher/status in the **existing Runs surface** over the existing BFF.
Reuses Prefect/gateway/MinIO; **no new service, runtime, or lifecycle stage**; the one-model-in-VRAM lease
is honored unchanged. Phase-gated like 005/006/007, validated against the full 001–007 suite each tier.

## Technical Context

**Language/Version**: Python 3.12 (gateway, per 007) + native WSL training venv (frozen GPU stack); Node
20+ (UI/BFF, unchanged). No new language or runtime.

**Primary Dependencies**: existing only — FastAPI/uvicorn/pydantic/boto3 (gateway), MLflow-skinny 3.x
(007), Prefect (ephemeral, native daemon), MinIO via boto3 (content addressing). **Validation is
hand-rolled stdlib + the data libs already present** (no new dependency; explicitly **no Great
Expectations**, and `pandera` out of scope by default — see Complexity Tracking). The frozen Blackwell
GPU/FT stack (torch cu128 / transformers / peft) is **untouched** — batch reuses the serving path, it
does not load models itself.

**Storage**: batch results are written **content-addressed** to MinIO (sha256 of result bytes), mirroring
`gateway/app/datasets.py` — `s3://<results-bucket>/<job>/<version>/{data,manifest.json}`. Datasets,
registry, and the MLflow backend are unchanged. Any batch-job status state reuses an existing store (no
new database — FR-136).

**Target Platform**: Win11 + WSL2 + Rancher Desktop. Gateway/MLflow/MinIO in Docker; training/bento/UI
native in WSL. A GPU-backed batch job runs where serving runs (through the lease); a CPU/tabular batch job
runs off-lease. Loopback-bound, fail-closed auth (005), BFF contract (004/005) — all unchanged.

**Project Type**: lifecycle-edge feature over 005/006/007 — adds a batch path (an **ephemeral Prefect flow
on the native daemon**, launched via the gateway `POST /batch`), a hand-rolled validation module, a
training-flow gate, and UI wire-in. New code, but inside the existing service/runtime boundaries.

**Performance Goals**: batch throughput is best-effort (single GPU, one model at a time); the goal is
**correctness + lease discipline**, not speed. Online `/infer` latency and the GPU-lock hold time MUST NOT
regress when batch is idle.

**Constraints**: one model in VRAM (Principle II) — batch holds/releases the lease, never pins a second
model; lightweight (Principle III) — hand-rolled validation, no heavy framework, no new service; full
lifecycle (Principle IV) — batch + the training gate complete the serving/training edges; loopback/auth/
BFF posture unchanged; no constitution amendment.

## Constitution Check

*GATE: Must pass before design. Re-check after. (Constitution v1.3.0.)*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Batch is a local job on existing infra; results on local MinIO; nothing leaves the host | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | GPU batch goes **through the existing VRAM lease** (acquire-once/release); CPU batch is off-lease; **no second model pinned** | ✅ honored |
| III. Lightweight Footprint | **Hand-rolled** validation (no Great Expectations / no new dep); batch reuses Prefect/gateway/MinIO; **no new service or DB** | ✅ |
| IV. Full Lifecycle Coverage | **Advanced** — completes the offline-serving edge (batch) and the training-readiness edge (validation gate); no stage added/dropped | ✅ strengthened |
| V. OSS & Swappable | Reuses MinIO/Prefect/MLflow behind clear interfaces; validation is a swappable in-process module | ✅ |
| VI. Reproducibility & Observability | Batch is a tracked MLflow run; results are content-addressed + manifested; validation reports recorded (fail-open) | ✅ |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1 batch, US2 gate, US3 wire-in), each re-validated on the target machine | ✅ |
| Workflow: "no new runtime/stage without amendment" | None introduced — batch is offline serving, validation is a training pre-flight; Prefect/gateway/MinIO/Python all pre-existing | ✅ no amendment |

**No amendment required.** 014 advances Principle IV within the existing constitution, honors Principle II
(the lease is the serialization point — batch is a tenant, not a new concurrency model), and stays
lightweight under Principle III (hand-rolled validation, no new service). Clean gate-check, mirroring
005/006/007.

> **Note on v1.4.0**: the prompt references a v1.4.0 gate-check; the repository constitution is **v1.3.0**
> (last amended 2026-06-28). 014 is gated against the **on-disk v1.3.0** text — Principles III + IV in
> particular — and introduces nothing requiring an amendment, so it is forward-compatible with any v1.4.0
> that does not narrow III/IV.

## Project Structure

### Source Code (delta over 007)

```text
mlops-lite/
├── gateway/
│   ├── app/
│   │   ├── batch.py                  # NEW: batch-scoring core — iterate dataset rows → score via the
│   │   │                             #      serving path (through the GPU lease) → content-addressed
│   │   │                             #      result on MinIO (mirrors datasets.py write pattern)
│   │   ├── validation.py             # NEW: hand-rolled lightweight validation — schema/null/range/
│   │   │                             #      label-balance/row-count → structured ValidationReport
│   │   └── routers/
│   │       ├── batch.py              # NEW: POST /batch (launch native flow), GET /batch/{id} status
│   │       └── validation.py         # NEW: POST /datasets/{name}/{version}/validate → report
│   ├── tests/                        # NEW: test_batch, test_validation (+ lease/regression assertions)
├── training/
│   └── flows/finetune.py            # MODIFIED: gate finetune_flow on validation BEFORE train_lora (FR-132)
├── training/flows/batch_infer.py    # NEW: ephemeral batch Prefect flow on the native daemon (grill A)
├── ui/
│   ├── app/ (datasets surface)       # MODIFIED: render the per-rule ValidationReport inline
│   └── app/ (runs surface)           # MODIFIED: batch-job launcher + live status + result link (no new tab)
│   └── app/api/ (BFF routes)         # MODIFIED: allowlist the new /batch + /validate proxy routes
└── docker-compose.yml               # MODIFIED (only if a new `batch-results` MinIO bucket is provisioned)
```

**Structure Decision**: keep batch-scoring core (`batch.py`) separate from the transport (the gateway
`POST /batch` route launches the **ephemeral Prefect flow** `batch_infer.py` on the native daemon — grill
A), so the scoring/result logic stays mechanism-clean. Validation lives in one `validation.py` (in-process,
no dependency) consumed by **both** the gateway validate route **and** the training gate — one
implementation, two call sites. UI is pure wire-in over the existing BFF.

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm the serving path can be called row-by-row under one lease acquisition
  (no per-row thrash) from the native daemon; confirm the gateway can launch the ephemeral Prefect batch
  flow (`POST /batch` → native flow); confirm a `batch-results` bucket (or reuse of an existing bucket
  prefix) on MinIO; confirm the validation default rule set + thresholds (required-columns/min-rows gate;
  null-rate/range/label-balance warn).
- **Phase 1 — Data validation + training gate (US2, P1)**: implement `validation.py` (hand-rolled rules +
  structured report); add the gateway validate route; **gate `finetune_flow` before `train_lora`**
  (FR-132). Re-validate: clean→pass, broken→fail, failing-dataset training rejected pre-LoRA, passing
  unchanged. Exit: SC-083 + SC-084. *(US2 leads — it is self-contained, protects GPU training immediately,
  and has no GPU-lease subtlety.)*
- **Phase 2 — Batch inference (US1, P1)**: implement `batch.py` (score every row through the serving path;
  GPU models acquire the lease once + release; CPU models off-lease; content-addressed result on MinIO);
  wire the **ephemeral Prefect flow** (`batch_infer.py`) launched via the gateway `POST /batch`; status
  reporting; record-and-continue per-row failures bounded by a configurable abort threshold; fail-open
  MLflow run. Re-validate:
  count-match, lease acquire-once/release, mid-batch `/infer` serialized, CPU off-lease. Exit: SC-081 +
  SC-082.
- **Phase 3 — UI wire-in (US3, P2)**: render the ValidationReport inline in Datasets; add the batch
  launcher + status + result link in the **existing Runs surface** (no new Batch tab); allowlist the new
  BFF proxy routes. Re-validate:
  report renders, job runs to a download, **API key absent from payloads**, 004/005 contract intact. Exit:
  SC-085.
- **Phase 4 — Cross-cutting regression**: full 001–007 no-regression sweep with 014 idle; GPU-lock hold
  time + online `/infer` latency unchanged. Exit: SC-086.

Cross-cutting: a no-regression sweep after the last tier, plus an explicit check that batch holds the VRAM
lease for **exactly** its duration (acquire-once, release-at-end) and never pins a second model.

## Complexity Tracking

| Decision | Why Needed | Simpler / Heavier Alternative Rejected Because |
|---|---|---|
| **Hand-rolled validation (NOT Great Expectations)** | Schema/null/range/label-balance/row-count is a few hundred lines of stdlib + the data libs already present; zero new dependency, fits Principle III | **Great Expectations** brings a large dependency tree + a config/expectation-suite/datadoc surface — heavyweight for a local single-operator readiness check; same "deliver the guarantee, lighter" call as **DVC→content-addressing** (datasets.py) and **Evidently→PSI** (drift). **`pandera`** is lighter than GE but still a new dep for rules we can express directly — deferred |
| **Batch goes through the existing GPU lease** | Principle II is the project's core constraint; batch must be a tenant of the one-VRAM-slot mutex, not a parallel loader | A dedicated batch model load (a second resident model, or a separate VRAM path) would violate one-model-in-VRAM — the exact invariant 014 must not break |
| **Content-addressed batch results (reuse datasets.py scheme)** | Immutable, idempotent, downloadable results with a manifest — already proven for datasets | A new bespoke result store / naming scheme would duplicate the content-addressing already in `datasets.py` for no gain |
| **One `validation.py`, two call sites (gateway route + training gate)** | Same rules must back the advisory register-time check **and** the hard training gate — one implementation avoids drift | Two separate validators (UI-side + training-side) would drift and disagree on a dataset's verdict |
| **Batch mechanism = ephemeral Prefect flow on the native daemon (grill A, resolved)** | The batch must hold the single GPU lease while iterating rows through the serving path — and the GPU/serving lives on the **native daemon**, consistent with training. Launched via the gateway `POST /batch` → native flow | A **gateway endpoint + background worker** was rejected: the **Docker gateway has no GPU**, so a worker there would have to **proxy every row** to the native daemon and could **not hold the lease** itself — defeating acquire-once/release-at-end. The native Prefect flow is the correct + consistent choice; `batch.py` stays mechanism-clean |
| **Batch is the offline serving edge, not a new stage (no amendment)** | Principle IV is advanced by completing existing-stage edges, not by adding a stage | Declaring batch a "new lifecycle stage" would force an amendment for what is offline serving + a training pre-flight |

## Grilled Items (resolved 2026-06-28)

1. **Batch job mechanism (A)** — **RESOLVED: ephemeral Prefect flow on the native daemon**, launched via
   the gateway (`POST /batch` → native flow), consistent with training. Acquire-once/release-at-end GPU
   lease, rows through the serving path, content-addressed results to MinIO; CPU/tabular off-lease.
   Rationale: the Docker gateway has no GPU, so a gateway-endpoint+worker would proxy every row and could
   not hold the lease. *(FR-128)*
2. **Validation rule set + thresholds, and hard-gate vs warn per rule (B)** — **RESOLVED: sensible
   defaults, configurable.** Default hard-gate-for-training: **required-columns/schema** + **min-row-count**.
   Default warn (configurable thresholds): **null-rate**, **value-range**, **label-balance**. Register-time
   validation is **advisory**. All rules/thresholds/dispositions configurable — defaults not immutable.
   *(FR-131, FR-132, FR-135)*
3. **Where batch results/status surface in the UI (C)** — **RESOLVED: the existing Runs surface** (no new
   Batch tab). Per-row failures are **recorded and the batch continues**, bounded by a **configurable abort
   threshold** (e.g. abort if >X% of rows fail). A dedicated Batch tab is a possible fast-follow, out of
   014 scope. *(FR-134, FR-129)*
