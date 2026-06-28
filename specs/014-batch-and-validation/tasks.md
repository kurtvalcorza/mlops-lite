---
description: "Task list for Batch Inference & Data-Validation Gates (014)"
---

# Tasks: Batch Inference & Data-Validation Gates

**Input**: Design documents from `specs/014-batch-and-validation/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the hardened, traced, refreshed
platform (005/006/007). Adds the two missing lifecycle edges — offline batch inference + data-validation
gates — over existing Prefect/gateway/MinIO. No new service/runtime/stage.

**Tests**: Re-run the relevant 001–007 integration suite per tier on the target machine before the next.
Task IDs continue the shared space (T256+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — GRILLED (2026-06-28), build-ready.**
> Scope: **batch inference + data-validation gates** completing the lifecycle edges. Firm FRs:
> FR-128–FR-137. SC-081–SC-086. Tasks T256–T275. **No constitution amendment** (advances Principle IV,
> stays lightweight under III). GPU/FT stack untouched — batch reuses the serving path through the
> existing VRAM lease.
>
> **Firm decisions:**
> 1. **Batch through the existing single-GPU lease** (acquire-once / release-at-end); CPU/tabular models
>    score **off-lease**. One model in VRAM throughout (Principle II) — non-negotiable.
> 2. **Content-addressed results on MinIO** (sha256, mirroring `datasets.py`) — immutable, idempotent,
>    downloadable, manifested.
> 3. **Hand-rolled lightweight validation** — schema/null/range/label-balance/row-count → structured
>    report. **NOT Great Expectations** (rejected in Complexity Tracking; `pandera` deferred). One
>    `validation.py`, two call sites (gateway route + training gate).
> 4. **Training is hard-gated** on validation **before** `train_lora`; register-time validation is
>    **advisory**.
> 5. **Reuse existing infra** — Prefect/gateway/MinIO; **no new service, runtime, DB, or lifecycle stage**.
>
> **Grilled decisions (2026-06-28):**
> - **(A) Batch mechanism = ephemeral Prefect flow on the native daemon**, launched via the gateway
>   (`POST /batch` → native flow), consistent with training; acquire-once/release-at-end lease, rows through
>   the serving path, content-addressed results to MinIO; CPU/tabular off-lease. The Docker gateway has no
>   GPU, so a gateway-endpoint+worker would proxy every row and could not hold the lease. *(FR-128)*
> - **(B) Validation = sensible defaults, configurable; hard-gate critical / warn soft.** Default
>   hard-gate-for-training: required-columns/schema + min-row-count; default warn (configurable thresholds):
>   null-rate, value-range, label-balance; register-time advisory. All rules/thresholds/dispositions
>   configurable. *(FR-131/FR-132/FR-135)*
> - **(C) Batch UX = minimal Runs surface + record-and-continue.** Status/results in the **existing Runs
>   tab** (no new Batch tab); per-row failures recorded and the batch continues, bounded by a **configurable
>   abort threshold**. *(FR-134/FR-129)*

---

## Phase 0 — Pre-flight (gates everything)

- [ ] **T256** [US2] Confirm the serving path can be invoked **row-by-row under a single GPU-lease
  acquisition** (no per-row lease thrash) from the native daemon and that a CPU/tabular model can score
  off-lease; confirm the gateway can launch the **ephemeral Prefect batch flow** (`POST /batch` → native
  flow); confirm a `batch-results` MinIO bucket (or an existing-bucket prefix) and the validation **default
  rule set** (required-columns/schema + min-row-count hard-gate; null-rate/range/label-balance warn, all
  configurable). (FR-128, FR-130, FR-136)

## Phase 1 — Data validation + training gate (US2, P1) → SC-083 + SC-084

- [ ] **T257** [US2] Implement `gateway/app/validation.py` — **hand-rolled lightweight** checks over a
  dataset version's bytes (the **sensible default rule set**): required columns/schema, per-column null
  rate, value ranges, label balance, row count → a structured `ValidationReport` (`passed` + per-rule
  `{name,passed,value,threshold}` + summary stats). Each rule carries a **gate-vs-warn disposition**
  (default: required-columns/schema + min-row-count = hard-gate; null-rate/range/label-balance = warn).
  **No Great Expectations / no new dependency.** All rules, thresholds, and dispositions **configurable**
  (defaults not immutable). (FR-131, FR-135)
- [ ] **T258** [US2] Add `gateway/app/routers/validation.py` — `POST /datasets/{name}/{version}/validate`
  → returns the report; resolve the dataset version via the existing `datasets.get_dataset` path. Wire
  the router in `main.py`. (FR-131, FR-135)
- [ ] **T259** [US2] Gate `training/flows/finetune.py`: in `finetune_flow`, run validation on the
  resolved dataset version **before** `train_lora` and **reject** a dataset that breaches a **hard-gate**
  rule (required-columns/schema or min-row-count, by default) — raise with the report attached, **no GPU
  acquisition, no wasted MLflow run**; **warn** rules (null-rate/range/label-balance) are recorded but do
  not block; a passing dataset proceeds unchanged. Dispositions configurable. (FR-132, FR-135)
- [ ] **T260** [US2] Record validation results to MLflow on the **fail-open** basis (006 precedent) —
  MLflow down never blocks the gate or the register-time advisory check. (FR-133)
- [ ] **T261** [P] [US2] `tests/test_validation`: clean dataset → `passed=true` + stats; broken datasets
  (empty / missing columns / over-null / under-min-rows) → `passed=false` naming each failed rule
  value-vs-threshold. (SC-083)
- [ ] **T262** [P] [US2] `tests/test_finetune_gate`: training launched on a **failing** dataset is
  rejected **before** `train_lora` (assert no GPU call / no MLflow run created); a **passing** dataset
  trains exactly as before 014. (SC-084)

## Phase 2 — Batch inference (US1, P1) → SC-081 + SC-082

- [ ] **T263** [US1] Implement `gateway/app/batch.py` — score **every** row of a dataset version through
  the **serving path**; **GPU-backed** model: acquire the single VRAM lease **once**, score all rows,
  **release** at the end (no per-row thrash, no second model pinned); **CPU/tabular** model: score
  **off-lease**. Collect per-row outputs + per-row failures (**record-and-continue**), aborting only if the
  failure rate breaches a **configurable abort threshold** (e.g. >X% of rows). (FR-128, FR-130, FR-129)
- [ ] **T264** [US1] Write the result **content-addressed** to MinIO (sha256 of result bytes, mirroring
  `datasets.py`): `data` + `manifest.json` (source dataset version, model + resolved registry version,
  counts in/out/failed, timing). Idempotent on identical input. (FR-129)
- [ ] **T265** [US1] Wire the **ephemeral Prefect flow** mechanism (grill A): `gateway/app/routers/batch.py`
  exposes `POST /batch` (submit → launch the native flow) + `GET /batch/{id}` (status), and
  `training/flows/batch_infer.py` is the ephemeral Prefect flow on the **native daemon** that holds the
  lease and scores. Report status queued → running → succeeded/failed; fail fast + structured error on
  unresolved model / missing dataset / empty dataset (no partial artifact). (FR-128)
- [ ] **T266** [US1] Log the batch run to MLflow (params: dataset version, model, counts; result URI) on
  the **fire-and-forget, fail-open** basis (006/007) — MLflow down never blocks the job. (FR-133)
- [ ] **T267** [P] [US1] `tests/test_batch`: one output per input row (count match); result artifact lands
  content-addressed + downloadable with a manifest; terminal status `succeeded`. (SC-081)
- [ ] **T268** [P] [US1] `tests/test_batch_lease`: GPU batch acquires the lease **once** + releases at end;
  an online `/infer` arriving mid-batch is **serialized** by the one-model-in-VRAM mutex; a CPU batch
  scores **off-lease** (no VRAM acquisition). **No second model pinned.** (SC-082, Principle II)

## Phase 3 — UI wire-in (US3, P2) → SC-085

- [ ] **T269** [US3] Datasets surface: render the per-rule **ValidationReport** inline (pass/fail + stats)
  for a selected dataset version, fetched via the BFF. (FR-134)
- [ ] **T270** [US3] Batch launcher + status in the **existing Runs surface** (grill C — no new Batch tab):
  submit dataset version + model; show live status (queued/running/succeeded/failed); show a **download
  link** to the MinIO result artifact on success; surface the per-row failure summary (record-and-continue).
  (FR-134, FR-129)
- [ ] **T271** [US3] BFF: **allowlist** the new `/batch*` + `/datasets/*/validate` proxy routes under the
  existing route allowlist + same-origin/CSRF guard (004/005); the gateway **API key MUST stay
  server-side** (absent from all browser payloads). (FR-134)
- [ ] **T272** [P] [US3] `tests/test_ui_batch_validation` (+ extend `test_ui_security`): report renders;
  job runs to a downloadable result; **API key absent from every browser-visible payload**; allowlist +
  origin guard + non-leaky errors intact. (SC-085)

## Phase 4 — Cross-cutting regression

- [ ] **T273** Full 001–007 keyed sweep green with 014 features **idle**: online `/infer`(+`/stream`),
  registry (alias promotion), content-addressed dataset registry, LoRA→GGUF loop, drift→retrain, 006/007
  tracing, BFF contract — all unchanged. (SC-086, FR-137)
- [ ] **T274** Confirm the GPU-lock **hold time** + online `/infer` **latency** are unchanged when batch is
  idle, and that a completed batch job has **released** the VRAM lease (no lingering resident model).
  (SC-082, SC-086)
- [ ] **T275** Commit the new modules/routes/tests, the UI wire-in, any `batch-results` bucket
  provisioning, and the updated `finetune.py` gate; record the resolved grill decisions (A/B/C) in this
  spec folder. (FR-136, FR-137)

---

## Dependencies & Execution Order

- **T256 (pre-flight) gates everything** — confirm the lease-once serving call + the mechanism/rule-set
  inputs before building.
- **US2 (validation + gate, T257–T262) leads** — self-contained, immediately protects GPU training, and
  has no GPU-lease subtlety; `validation.py` is also reused by US3.
- **US1 (batch, T263–T268)** is the higher-risk tier (GPU-lease discipline) — do it after US2 so a
  regression is isolated to the batch path.
- **US3 (T269–T272)** is wire-in over US1/US2 contracts; **T273–T275 land last** (need every tier).

### Constitution gates (re-check each phase) — v1.3.0
- **Principle II honored**: GPU batch goes **through the existing lease** (acquire-once/release); CPU
  batch off-lease; **no second model in VRAM** (verify in T268/T274).
- **Principle III**: hand-rolled validation (no Great Expectations / no new dep); no new service/DB.
- **Principle IV advanced**: batch = offline serving edge; validation gate = training pre-flight — no new
  stage → **no amendment**.

## Implementation Strategy

1. **Validation + training gate first** → fast-fail the most expensive op. **Stop and validate.**
2. **Batch inference** → score-through-the-lease + content-addressed results via the ephemeral Prefect
   flow (gateway `POST /batch` → native daemon).
3. **UI wire-in** → report + launcher over the existing BFF, key server-side.
4. Each phase re-runs the relevant 001–007 tests on the target machine; never regress; never pin a second
   model in VRAM.

## Out of Scope (recorded)
- **A standing batch service / job-queue cluster** — ephemeral job on existing infra only (Principle I/III).
- **Great Expectations / heavy validation framework** — hand-rolled, lightweight (Principle III);
  `pandera` deferred.
- **Multi-model / multi-GPU batch parallelism** — one model in VRAM at a time (Principle II).
- **A new lifecycle stage or constitution amendment** — 014 completes existing-stage edges.
- **Online/streaming micro-batching of `/infer`** — 014 is offline batch over a dataset version.
