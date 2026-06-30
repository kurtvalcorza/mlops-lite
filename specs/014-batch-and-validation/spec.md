# Feature Specification: Batch Inference & Data-Validation Gates

**Feature Branch**: `014-batch-and-validation`

**Created**: 2026-06-28

**Status**: **BUILT & MERGED (2026-06-29, PR #17)** — US1–US3. Offline batch inference (`POST /batch`
+ `GET /batch/{batch_id}` → native `training/flows/batch_infer.py` ephemeral Prefect flow, single GPU lease,
content-addressed MinIO results; CPU/tabular off-lease) + hand-rolled data-validation gates
(`gateway/app/validation.py`) gating `finetune_flow` before `train_lora` + advisory
`POST /datasets/{name}/{version}/validate`; UI wire-in + BFF allowlist. No new dep/service/stage. See
tasks.md status block. (Was DRAFT — GRILLED 2026-06-28.)

**Input**: Roadmap-planned lifecycle-completion increment. The platform serves inference **online/sync
only** (`/infer`, `/infer/stream`) — there is no way to score a whole dataset offline. And training
(`training/flows/finetune.py`) consumes a dataset version with **no readiness check** — a malformed,
empty, or schema-drifted dataset fails deep inside the LoRA loop instead of fast at the edge. 014 closes
both edges: it adds **offline batch inference** (score a registered MinIO dataset version against a
served model, write results back to MinIO) and **lightweight data-validation gates** (schema/null/range/
label-balance/row-count checks before training), surfaced in the operator console.

> **Scope note**: 014 advances **Principle IV (Full Lifecycle Coverage)** — it completes the two
> lifecycle edges that online-only inference and ungated training leave open. It reuses the **existing**
> Prefect/gateway/MinIO building blocks: batch is a job (an **ephemeral Prefect flow on the native daemon**,
> launched via the gateway `POST /batch` — grill A), validation is **hand-rolled
> lightweight** Python over the dataset bytes. Requirement IDs continue the shared space (FR-128+,
> SC-081+, tasks T256+). **No constitution amendment** — no new lifecycle stage (batch is the offline
> form of the existing serving stage; validation is a pre-flight on the existing training stage), no new
> service, no new runtime, and the one-model-in-VRAM lease is honored unchanged.

> **Hard boundary (NON-NEGOTIABLE)**: batch inference against a **GPU-backed** model goes **through the
> existing GPU lease** — it acquires, holds, and releases the single VRAM slot exactly like any online
> tenant (Principle II). A batch job MUST NOT pin a second model in VRAM, MUST NOT bypass the lease, and
> MUST release promptly when done. **CPU-backed** models (tabular) score **off-lease**, never touching
> VRAM.

> **Validation library boundary**: validation is **hand-rolled lightweight** Python (schema/columns,
> null rate, value ranges, label balance, row count) over the dataset bytes — **NOT Great Expectations**
> (too heavy: a large dependency tree + a config/datadoc surface that cuts against Principle III), and
> not (in the default scope) `pandera`. This mirrors the project's prior "deliver the same guarantee,
> lighter" precedents — **DVC → content-addressing** (datasets.py) and **Evidently → PSI** (drift). See
> Complexity Tracking.

> **Grilled decisions (2026-06-28)** — the three open grill items are resolved:
> - **(A) Batch mechanism = ephemeral Prefect flow on the native daemon**, launched via the gateway
>   (`POST /batch` → native flow), consistent with training. The flow acquires the single GPU lease **once**
>   and releases at batch end, iterates rows through the serving path, and writes content-addressed results
>   to MinIO; CPU/tabular models score off-lease. Rationale: the Docker gateway has no GPU, so a
>   gateway-endpoint+worker would proxy every row to the native daemon and could not hold the lease — the
>   native Prefect flow is the correct + consistent choice. *(FR-128)*
> - **(B) Validation = sensible default rules + thresholds, configurable; hard-gate critical / warn soft.**
>   Default hard-gate-for-training rules: **required-columns/schema** + **min-row-count**. Default warn
>   rules (above configurable thresholds): **null-rate**, **value-range**, **label-balance**. Register-time
>   validation stays **advisory**. All rules, thresholds, and gate-vs-warn dispositions are **configurable**
>   — defaults are not immutable. *(FR-131/FR-132/FR-135)*
> - **(C) Batch UX = minimal Runs surface + record-and-continue.** Batch job status/results surface in the
>   **existing Runs tab** (no new Batch tab). Per-row failures are **recorded and the batch continues**,
>   bounded by a **configurable abort threshold** (e.g. abort if >X% of rows fail). *(FR-134/FR-129)*

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Offline batch inference over a dataset version (Priority: P1)

The operator submits a registered MinIO dataset version plus a target model (the `@serving` LLM/vision
model, or a registered version), the platform scores **every** record offline as a job, and writes the
results back to MinIO as a content-addressed artifact the operator can download — without tying up the
interactive `/infer` path.

**Why this priority**: This is the bigger lifecycle gap — the platform can serve one prompt at a time but
cannot score a corpus. Batch is the standard MLOps complement to online serving and the higher-value half
of 014, so it leads.

**Independent Test**: Register a small JSONL dataset version, submit a batch-inference job (`POST /batch`,
which launches an ephemeral Prefect flow on the native daemon) against the
`@serving` model, and observe: the job runs to completion as a tracked run, every input row produces an
output row, the result artifact lands in MinIO (downloadable), the GPU lease is acquired once and
released at the end (one model in VRAM throughout), and the online `/infer` path is unaffected during the
run.

**Acceptance Scenarios**:

1. **Given** a registered dataset version and a served GPU-backed model, **When** a batch-inference job
   is submitted, **Then** the job acquires the GPU lease, scores every input row, releases the lease, and
   writes a results artifact to MinIO with one output per input (count matches), reporting job
   status/progress.
2. **Given** a **CPU-backed** (tabular) registered model, **When** a batch job is submitted, **Then** it
   scores **off-lease** (no VRAM acquisition) and writes results identically.
3. **Given** a batch job holding the GPU lease, **When** an online `/infer` request arrives, **Then** the
   one-model-in-VRAM mutex serializes them (one waits) — neither corrupts the other, and the VRAM
   invariant holds (Principle II).
4. **Given** a submitted job, **When** the operator polls status, **Then** they see queued → running →
   succeeded/failed with the result artifact URI on success and a clear error on failure (e.g. unresolved
   model, unreadable dataset).

---

### User Story 2 — Data-validation gates before training (Priority: P1)

Before a training run consumes a dataset version, the platform runs a **lightweight readiness check**
(schema/columns present, null rate per column, value ranges, label balance, minimum row count) and
**gates** the run on the result — a failing dataset is rejected fast with a clear, structured report
instead of crashing deep inside the LoRA loop.

**Why this priority**: This is the DIMER `analysis-worker` pattern the platform lacks — a dataset-
readiness check that fails fast at the edge. It directly protects the most expensive operation (GPU
training) and the feedback loop (drift → retrain must not retrain on garbage), so it is P1 alongside US1.

**Independent Test**: Run validation on a clean dataset version (passes, structured report) and on a
deliberately-broken one — empty, missing the required instruction/response columns, or all-null — (fails
with a report naming the failed rule). Then submit a training run on the broken dataset and confirm it is
**rejected before** the LoRA loop starts, with the report attached; a training run on the clean dataset
proceeds unchanged.

**Acceptance Scenarios**:

1. **Given** a clean dataset version, **When** validation runs, **Then** it returns a structured report
   (per-rule pass/fail + summary stats: row count, null rates, label balance) with overall `passed=true`.
2. **Given** a dataset that is empty, missing required columns, exceeds the null-rate threshold, or falls
   below the minimum row count, **When** validation runs, **Then** the report has `passed=false` and
   names each failed rule with the offending value vs. the threshold.
3. **Given** the training flow gated on readiness, **When** a run is launched on a **failing** dataset,
   **Then** the run is rejected **before** `train_lora` (no GPU acquisition, no MLflow run wasted) with
   the validation report surfaced.
4. **Given** the training flow, **When** a run is launched on a **passing** dataset, **Then** training
   proceeds exactly as before 014 (no behavior change on the happy path).

---

### User Story 3 — Surface validation reports & batch jobs in the console (Priority: P2)

The operator console surfaces a **dataset-validation report** (in the Datasets surface) and a
**batch-job launcher + status** in the **existing Runs surface** (no new Batch tab — grill C), so both new
capabilities are operable from the UI, not just the API/CLI.

**Why this priority**: 014's value is realized when the operator can launch a batch job and read a
validation verdict from the console. It depends on US1/US2 existing, so it follows them; it is wire-in
over a stable contract, hence P2.

**Independent Test**: From the console, trigger validation on a dataset version and read the pass/fail
report inline; launch a batch-inference job and watch it move queued → running → succeeded with a
downloadable result link — all over the BFF, with the gateway API key never present in browser-visible
payloads (the 004/005 BFF security contract holds unchanged).

**Acceptance Scenarios**:

1. **Given** a registered dataset version in the Datasets surface, **When** the operator requests
   validation, **Then** the per-rule report (pass/fail + stats) renders inline, fetched via the BFF
   allowlist (no key leak).
2. **Given** the batch launcher, **When** the operator submits a dataset version + model, **Then** the
   job appears with live status (queued/running/succeeded/failed) and, on success, a download link to the
   MinIO result artifact.
3. **Given** the BFF security contract (004/005), **When** the new validation/batch routes are exercised,
   **Then** the API key is absent from all browser-visible payloads, the route allowlist + same-origin/
   CSRF guard hold, and errors stay non-leaky.

---

### Edge Cases

- **Batch holds the GPU lease**: a long batch run must acquire the lease once, score, and release —
  **not** re-acquire per row (lease thrash) and **not** hold it after completion. An online `/infer`
  arriving mid-batch waits on the mutex; both must remain correct (FR-128, FR-130).
- **CPU vs GPU model routing**: a tabular/CPU model scores off-lease; a GPU model goes through the lease.
  The job must route by the model's kind, never pinning a second model in VRAM (FR-130, Principle II).
- **Result write-back is content-addressed**: batch results land on MinIO under the existing content-
  addressed scheme (sha256 of the result bytes), so re-running an identical job is idempotent and results
  are immutable + downloadable, consistent with datasets.py (FR-129).
- **Validation is a hard gate for training, advisory on register**: training is **blocked** on a failing
  report (fail-fast); validation **on register** is **advisory** (warn + record), so registering an
  imperfect dataset is still allowed but flagged. Per the grill: **required-columns/schema** and
  **min-row-count** **hard-gate** training; **null-rate**, **value-range**, and **label-balance** **warn**
  above configurable thresholds; all rules + their gate-vs-warn disposition are **configurable** (FR-132,
  FR-135).
- **Unresolvable / empty inputs**: a batch job against an unresolved model alias, a missing dataset
  version, or an empty dataset fails fast with a structured error — never a partial/half-written result
  artifact (FR-128, FR-131).
- **Malformed records mid-dataset**: a batch run **records per-row failures and continues** (bad JSON,
  missing field) in the result artifact rather than aborting the whole job, unless the failure rate
  breaches a **configurable abort threshold** (e.g. abort if >X% of rows fail), summarizing at the end
  (FR-129).
- **Fail-open tracing unchanged**: batch runs and validation gates log to MLflow on the existing
  fire-and-forget, fail-open basis (006 precedent) — MLflow being down never blocks a batch job or a
  training gate (FR-133).
- **No regression**: the full 001–007 suite still passes — online `/infer`(+`/stream`), the registry,
  the content-addressed dataset registry, the LoRA→GGUF training loop, drift→retrain, and the
  one-model-in-VRAM mutex behave identically when 014 features are idle (SC-086).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-128**: The platform MUST support **offline batch inference**: submit a registered MinIO dataset
  version + a target model (the `@serving` model or a registered version) → score **every** record →
  write a results artifact back to MinIO. The job MUST report status (queued → running → succeeded/
  failed) and MUST fail fast with a structured error on an unresolved model, a missing dataset version,
  or an empty dataset (no partial/half-written artifact). The job **mechanism** is an **ephemeral Prefect
  flow on the native daemon** (consistent with training — the batch runs natively where the GPU/serving
  lives), **launched via the gateway** (`POST /batch` → native flow); CPU/tabular models score off-lease.
  The flow acquires the single GPU lease **once** and releases at batch end, iterates rows through the
  serving path, and writes content-addressed results to MinIO (the Docker gateway has no GPU, so a
  gateway-endpoint+worker would proxy every row to the native daemon and could not hold the lease — the
  native Prefect flow is the correct + consistent choice).
- **FR-129**: Batch results MUST be written to MinIO **content-addressed** (sha256 of the result bytes,
  mirroring `datasets.py`), as an immutable, downloadable artifact with a manifest recording the source
  dataset version, the model + resolved registry version, row counts (in/out/failed), and timing.
  Identical re-runs are idempotent (same result version). Per-row failures (bad JSON, missing field) are
  **recorded and the batch continues** — written to the artifact and summarized, not fatal to the whole
  job — unless the failure rate breaches a **configurable abort threshold** (e.g. abort if >X% of rows
  fail).
- **FR-130**: A **GPU-backed** batch job MUST go **through the existing single-GPU lease** — acquire the
  VRAM slot **once**, score, and **release** at completion — honoring the one-model-in-VRAM invariant
  (Principle II) exactly like an online tenant; an online `/infer` arriving mid-batch is serialized by
  the same mutex. A **CPU-backed** (tabular) model MUST score **off-lease** (no VRAM acquisition). A batch
  job MUST NOT pin a second model in VRAM or hold the lease after completion.
- **FR-131**: The platform MUST provide **lightweight, hand-rolled data validation** over a dataset
  version's bytes, computing at minimum a **sensible default rule set**: **schema/required columns
  present**, **per-column null rate**, **value ranges** (numeric bounds), **label balance** (class
  distribution), and **row count**, against **configurable thresholds**. It MUST return a **structured
  report**: overall `passed` boolean + per-rule pass/fail + summary statistics, naming each failed rule
  with the offending value vs. its threshold. All rules, thresholds, and each rule's gate-vs-warn
  disposition are **configurable** — exact numbers are defaults, not immutable. Validation MUST NOT use
  Great Expectations (see Complexity Tracking); `pandera` is also out of scope by default (hand-rolled
  preferred).
- **FR-132**: The training flow (`training/flows/finetune.py`) MUST be **gated on readiness**: a run on a
  **failing** dataset version MUST be **rejected before** the LoRA loop (`train_lora`) starts — no GPU
  acquisition and no wasted MLflow run — with the validation report surfaced. A run on a **passing**
  dataset MUST proceed exactly as before 014 (no happy-path behavior change). The default **hard-gate**
  rules for training are **required-columns/schema** and **min-row-count** (a breach rejects the run);
  **null-rate**, **value-range**, and **label-balance** **warn** above their configurable thresholds
  rather than gating. Validation **on dataset register** is **advisory** (warn + record). All rule
  dispositions (gate vs. warn) are **configurable**.
- **FR-133**: Batch jobs and validation gates MUST log to MLflow on the **existing fire-and-forget,
  fail-open** basis (006 precedent): a batch run is a tracked run (params: dataset version, model, row
  counts; the result artifact URI), and a validation result is recorded — but MLflow being unreachable
  MUST NOT block a batch job or a training gate. Toggles consistent with the 006 tracing flags.
- **FR-134**: The operator console MUST surface (a) a **dataset-validation report** in the Datasets
  surface (per-rule pass/fail + stats, rendered inline) and (b) a **batch-job launcher + status** in the
  **existing Runs surface** (no new Batch tab; submit dataset version + model; live status; result
  download link on success). Per-row failures are **recorded and the batch continues**, bounded by a
  **configurable abort threshold** (FR-129). Both MUST go through the **existing BFF** under the route
  allowlist + same-origin/CSRF guard, with the gateway API key **absent** from all browser-visible
  payloads (004/005 contract unchanged).
- **FR-135**: Validation MUST run **before a training run** (the hard gate, FR-132) and **MAY** run on
  **dataset register** (advisory). The default rule set is **required-columns/schema** + **min-row-count**
  (hard-gate training) and **null-rate** + **value-range** + **label-balance** (warn above configurable
  thresholds); register-time validation is advisory. The **rule set, thresholds, and per-rule gate-vs-warn
  disposition are all configurable** — the defaults are sensible starting points, not immutable numbers.
- **FR-136**: 014 MUST reuse the **existing** infrastructure — Prefect (ephemeral, native daemon), the
  gateway, MinIO (content-addressed) — and MUST add **no new service, no new runtime, and no new
  lifecycle stage**. Validation is in-process Python (hand-rolled, lightweight). Any persistent batch-job
  state (status) reuses an existing store; no new database.
- **FR-137**: 014 MUST NOT regress 001–007: online `/infer`(+`/infer/stream`), the registry (alias
  promotion), the content-addressed dataset registry, the LoRA→GGUF training loop, drift→retrain, 006/007
  tracing, the BFF security contract, and the one-model-in-VRAM mutex MUST behave identically when the
  014 features are idle. Each user story MUST be validated against the relevant suite on the target
  machine before the next.

### Key Entities *(include if feature involves data)*

- **BatchJob**: an offline scoring job — `{dataset_name@version, model (alias or registry version),
  status (queued|running|succeeded|failed), row counts (in/out/failed), result artifact URI, timing,
  mlflow run id}`. GPU-backed jobs hold the single VRAM lease for their duration; CPU-backed jobs run
  off-lease.
- **BatchResult**: the content-addressed MinIO artifact (sha256 of the result bytes) + manifest (source
  dataset version, model + resolved registry version, counts, timing) — immutable + downloadable, mirroring
  the dataset registry layout.
- **ValidationReport**: a structured readiness verdict for a dataset version — `{passed: bool, rules:
  [{name, passed, value, threshold}], stats: {row_count, null_rates, label_balance, ...}}` — the hard gate
  on training (FR-132), advisory on register.
- **ValidationRuleSet**: the configurable set of lightweight checks (required columns, null-rate, value
  ranges, label balance, min row count) + thresholds + per-rule gate-vs-warn disposition. Defaults:
  required-columns/schema + min-row-count hard-gate training; null-rate + value-range + label-balance
  warn. All configurable.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-081**: A batch-inference job over a registered dataset version produces **exactly one output per
  input row** (counts match), written as a content-addressed, downloadable MinIO artifact with a manifest
  (source dataset version + model + counts + timing), and reports terminal status `succeeded`.
- **SC-082**: A GPU-backed batch job acquires the single VRAM lease **once** and releases it at
  completion; an online `/infer` arriving mid-batch is correctly serialized (one model in VRAM
  throughout — Principle II holds); a CPU-backed batch job scores **off-lease** (no VRAM acquisition).
- **SC-083**: Validation on a clean dataset returns `passed=true` with a structured per-rule report +
  summary stats; validation on a deliberately-broken dataset (empty / missing columns / over-null /
  under-min-rows) returns `passed=false` naming each failed rule with value-vs-threshold.
- **SC-084**: A training run launched on a **failing** dataset version is **rejected before** the LoRA
  loop (no GPU acquisition, no wasted MLflow run) with the validation report surfaced; a run on a
  **passing** dataset proceeds exactly as before 014.
- **SC-085**: From the operator console (over the BFF), the operator can read a dataset-validation report
  inline and launch + monitor a batch job to a downloadable result — with the gateway API key absent from
  every browser-visible payload and the 004/005 BFF security contract intact.
- **SC-086**: No regression — the full 001–007 suite passes with 014 features idle: online `/infer`
  (+`/stream`), the registry, the content-addressed dataset registry, the LoRA→GGUF loop, drift→retrain,
  006/007 tracing, and the one-model-in-VRAM mutex are unchanged.

## Assumptions

- **Batch is the offline form of the existing serving stage, not a new stage** — it reuses serving + the
  GPU lease, so it advances Principle IV without an amendment. Validation is a pre-flight on the existing
  training stage, likewise not a new stage.
- **Content addressing fits batch results** — the dataset registry's sha256 scheme (datasets.py) applies
  directly to result artifacts: immutable, idempotent, downloadable. No new storage primitive.
- **Hand-rolled validation is enough** — the rule set (schema/null/range/label-balance/row-count) covers
  the readiness failures that matter for a local single-operator MLOps loop; the DVC→content-addressing
  and Evidently→PSI precedents show the project favors a light hand-rolled equivalent over a heavy
  framework.
- **The GPU lease already exists and is the serialization point** — batch plugs into the same
  one-model-in-VRAM mutex the online path uses; 014 adds a tenant, not a new concurrency model.
- **Single local operator, unchanged posture** — loopback binding, fail-closed gateway auth, and the BFF
  contract (004/005) all stand; 014 adds routes inside them.

## Non-Goals

- **A standing batch service / job queue cluster** — batch is an ephemeral job on existing infra (Prefect
  on the native daemon and/or a gateway worker), not a new always-on service or distributed queue
  (Principle I/III).
- **Great Expectations (or a heavy validation framework)** — explicitly rejected for weight (Principle
  III); `pandera` also out of scope by default. Validation is hand-rolled, lightweight Python.
- **Multi-model / multi-GPU batch parallelism** — one model in VRAM at a time (Principle II); batch does
  not parallelize across models or pin a second model.
- **A new lifecycle stage or a constitution amendment** — 014 completes the edges of the existing serving
  and training stages; no stage is added or dropped.
- **Streaming/online batch (micro-batching the live path)** — 014 is offline batch over a dataset
  version, not a throughput optimization of `/infer`.
- **Rich validation profiling / data-doc generation** — no Great-Expectations-style datadocs; the report
  is a compact structured verdict, not a profiling site.
- **A dedicated Batch tab in the console** — batch surfaces in the existing Runs tab (grill C); a
  standalone Batch surface is a possible **fast-follow**, not part of 014.
