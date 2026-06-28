---
description: "Task list for Model-Quality Monitoring with Ground Truth (013)"
---

# Tasks: Model-Quality Monitoring with Ground Truth

**Input**: Design documents from `specs/013-quality-monitoring/`

**Prerequisites**: plan.md (required), spec.md (required); builds on the existing input-PSI monitoring
(`gateway/app/monitoring.py` + `routers/monitor.py`, the drift→retrain loop) and reuses **011's metric
libs**. Extends monitoring from **input** drift to **output / ground-truth quality** — additive, no
lifecycle/UI/API removal.

**Tests**: Re-run the relevant prior integration suite (especially the input-PSI drift + retrain path) per
phase on the target machine before the next. Task IDs continue the shared space (T238+).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- File paths follow the structure in [plan.md](./plan.md).

---

> **Status (2026-06-28):** **DRAFT — GRILLED (2026-06-28), build-ready.**
> Scope: **model-quality monitoring with ground truth** — log predictions + (delayed) labels, compute
> windowed per-modality quality, fire the existing retrain on a quality breach (complementing input-PSI).
> Mostly new code, **no heavy deps**, **no new service/runtime**, GPU/FT stack **frozen**. No constitution
> amendment (advances Principle VI; completes Principle IV's output-side loop). Tasks T238–T255.
>
> **Decided / firm (from spec):**
> 1. **Prediction logging is fire-and-forget, off the request path, fail-open** (the 006-tracing pattern):
>    prediction id + model version per `/infer`, `/infer/stream`, `/vision/classify`; logging-store outage
>    never breaks serving; **no second resident model**.
> 2. **Quality is a windowed aggregate over labeled pairs**, per modality, **reusing 011's metric libs**
>    (accuracy/F1/WER/…); thin window ⇒ *insufficient data*, no false breach.
> 3. **Records ride the existing stores** (MinIO `results` bucket prefixes and/or Postgres) — **no new
>    datastore**; keyed by `prediction_id`, tagged with `model_version`.
> 4. **Quality breach reuses `_launch_retrain`** (the existing trigger), counter labeled to distinguish the
>    **quality** signal from the **input-PSI** signal; fail-soft on trainer-down.
> 5. **Grafana dashboard is EXTENDED** (model-quality panel beside the input-drift panels), not replaced.
> 6. **Input-PSI drift path stays UNCHANGED** (`monitoring.py` untouched); quality lives in a sibling
>    `quality.py` so a regression bisects input vs output.
> 7. **GPU/FT stack FROZEN**; **no heavy deps** (no Evidently/pandas/scipy).
>
> **Grilled decisions (2026-06-28):**
> a. **Label ingestion = API endpoint + thin batch wrapper** — `POST /monitor/labels` (attach a label to a
>    `prediction_id`); a thin batch client (mirroring `data/register_dataset.py`) loops it for bulk/delayed
>    labels. Covers online + bulk in one path — T241.
> b. **Windowing = sliding count-based (last N labeled pairs), on-demand recompute + optional periodic, N
>    configurable** — recompute via `POST /monitor/quality/check` (mirroring `POST /monitor/check`).
>    Deterministic + robust to sparse delayed labels (thin window ⇒ *insufficient data*, no false breach) — T244.
> c. **Threshold = relative-to-baseline, configurable** — breach = windowed metric drops > configurable X%
>    below the model's registered/eval (011) baseline; per-modality metrics reuse 011's libs — T243/T247.
> d. **Combine policy = OR + cooldown, configurable** — either input-PSI breach OR quality breach fires
>    `_launch_retrain`, with a debounce/cooldown to prevent retrain storms; each signal independently
>    observable — T249.

---

## Phase 0 — Pre-flight (gates everything)

- [ ] **T238** [US1] Confirm **011's metric libs** import as a library and cover the needed modalities
  (classification accuracy/F1, ASR-style WER); confirm the MinIO `results` bucket + the existing Postgres
  are reachable for new prediction/label records; decide the store layout (default: `results` prefixes
  `predictions/`, `labels/`, `quality/`, mirroring `drift/`). No GPU touched. (FR-121, FR-122)

## Phase 1 — Prediction + (delayed) label logging (US1, P1) → SC-075 + SC-076

- [ ] **T239** [US1] Add `gateway/app/quality.py` (sibling of `monitoring.py`): a **fire-and-forget,
  fail-open** prediction logger following the **006 tracing pattern** (background worker, lazy init, work
  **outside** `_gpu_lock`). Writes a `PredictionRecord` — `prediction_id` (uuid), `model_version` (resolved
  at serve time), modality, input ref, prediction/output (gated by a capture toggle à la
  `MLFLOW_TRACE_CAPTURE_IO`), timestamp — to the existing store. (FR-119, FR-121)
- [ ] **T240** [US1] Wire prediction logging into `routers/infer.py`, `routers/stream.py` (SSE — frame-safe,
  byte-identical stream), and `routers/vision.py` — off the request path; a logging failure MUST NOT alter
  or break the served response. (FR-119)
- [ ] **T241** [US1] Add the **label-ingestion path** to `routers/monitor.py`: the primary primitive is
  **`POST /monitor/labels`** (attach a `LabelRecord` to a `prediction_id`), plus a **thin batch client**
  (mirroring `data/register_dataset.py`) that loops the endpoint for bulk/delayed labels — covering online +
  bulk in one path. Attach `LabelRecord`s **by `prediction_id`**, supporting **delayed** arrival;
  reject/ignore unknown or duplicate ids cleanly (no overwrite of served history). Unlabeled predictions
  remain **pending**. (FR-120, FR-121)
- [ ] **T242** [P] [US1] Tests: serve N inferences → N prediction records with unique ids + model version;
  logging-store-down ⇒ serving response **identical** (fail-open); submit delayed labels → join by id;
  **late-arriving** label (after the prediction left the current window) still attaches; unknown/duplicate
  label rejected; unlabeled stay pending. (SC-075, SC-076)

## Phase 2 — Windowed quality computation (US2, P1) → SC-077 + SC-078

- [ ] **T243** [US2] In `quality.py`, implement **windowed quality compute**: join predictions↔labels over a
  **sliding count-based window** (the **last N labeled pairs**, **N configurable**), score **labeled pairs
  only** per modality via **011's metric libs** (accuracy/F1 for classification, WER for ASR text); also
  resolve the per-modality **011 registered/eval baseline** metric for the relative-to-baseline threshold
  (see T247); exclude unlabeled; thin window ⇒ **insufficient data**. Pure-Python, **no heavy deps**.
  (FR-122, FR-123, FR-125)
- [ ] **T244** [US2] Add a **`POST /monitor/quality/check`** on-demand recompute endpoint to `monitor.py`
  (mirroring the existing `POST /monitor/check` PSI pattern; **N configurable**, optionally also runnable
  periodically). Export **Prometheus gauges** for quality (labeled by `model_version` / modality — the
  output-side complement to `gateway_drift_score`/`gateway_dataset_drift`) and write a **`QualityReport`**
  to the MinIO `results` bucket (`quality/` prefix, drift-report shape: model version, modality, metric +
  value or *insufficient data*, labeled-pair count, window bounds, breach flag, timestamp). Add a
  `GET /monitor/quality` read endpoint (recent quality reports, newest first) in `monitor.py`. (FR-122, FR-123)
- [ ] **T245** [US2] **Extend** `infra/grafana/.../mlops-lite.json` with a **model-quality panel** (per
  modality / model version) placed **beside** the existing input-drift panels (do not remove/relayout the
  PSI panels). (FR-124)
- [ ] **T246** [P] [US2] Tests: windowed quality over labeled pairs == a hand-computed expected value;
  unlabeled excluded; thin window ⇒ *insufficient data* (no breach, no misleading score); gauge updates on
  scrape; quality report present in `results`; the extended dashboard JSON validates and keeps the input-
  drift panels. (SC-077, SC-078)

## Phase 3 — Quality-breach → retrain + combine policy (US3, P2) → SC-079

- [ ] **T247** [US3] Add a per-modality **relative-to-baseline degradation threshold**: mark a window's
  `QualityReport` as **breached** when the windowed metric drops more than a **configurable X%** below the
  model's **registered/eval (011) baseline** metric (per-modality metrics reuse 011's libs; X% configurable).
  (FR-125)
- [ ] **T248** [US3] On a breach (and if a retrain spec is supplied), fire the **existing** `_launch_retrain`
  → training daemon; increment the retrain counter with a label distinguishing the **quality** trigger from
  the **input-PSI** trigger; **fail-soft** (trainer unreachable ⇒ quality report still stands), respecting
  the existing one-trainer-busy / one-model-in-VRAM guards. (FR-125)
- [ ] **T249** [US3] Implement the **combine policy** for the two breach signals — **OR + configurable
  cooldown/debounce**: either the input-PSI breach OR the quality breach fires `_launch_retrain`, with a
  cooldown window preventing retrain storms (double-firing). Keep each signal **independently observable**
  (separate gauges/labels) so an operator sees which fired. (FR-126)
- [ ] **T250** [P] [US3] Tests: below-threshold window ⇒ retrain launched **exactly once** (counter +1,
  labeled *quality*), trainer-down ⇒ fail-soft (report stands); above-threshold ⇒ no launch; the combine
  policy behaves per spec (neither-alone vs both) and each signal is independently observable. (SC-079)

## Phase 4 — Cross-cutting regression → SC-080

- [ ] **T251** [P] Re-validate the **input-PSI** path unchanged: existing drift compute, `gateway_drift_score`
  / `gateway_dataset_drift`, drift reports in `results`, and `POST /monitor/check` retrain all behave
  identically (`monitoring.py` untouched). (SC-080)
- [ ] **T252** [P] UI no-regression: the six tabs + the BFF contract (allowlist, origin guard, `[::1]`,
  non-leaky errors, key absent from payloads) unchanged; any quality surfacing is a thin read, not a new
  datastore/tool. (SC-080)
- [ ] **T253** [P] Serving no-regression: SSE framing byte-identical; **no second resident model**; the
  one-model-in-VRAM mutex + GPU-lock hold time + inference latency unchanged with logging on. (SC-080)
- [ ] **T254** [P] Confirm **no heavy deps** added (no Evidently/pandas/scipy) and the **GPU/FT stack
  frozen** (no torch-family movement); footprint stays within Principle III. (SC-080)
- [ ] **T255** Full prior keyed sweep green with model-quality monitoring in place; quality reports +
  extended dashboard committed; the two complementary breach signals documented and independently
  observable. (SC-079, SC-080)

---

## Dependencies & Execution Order

- **T238 (pre-flight) gates everything** — never build quality compute without confirming 011's libs +
  store reachability and the store layout.
- **US1 (logging, T239–T242)** is the data-capture foundation; **US2 (compute, T243–T246)** depends on
  logged predictions + labels; **US3 (trigger/combine, T247–T250)** depends on US2's quality value.
- **Phase 4 (regression, T251–T255)** lands last — needs every tier in place; T255 is the final gate.

### Constitution gates (re-check each phase)
- Principle II untouched: quality is CPU-side, off the request path; **no second resident model**; VRAM
  mutex unchanged (verify in T253).
- Principle III honored: **no heavy deps** — pure-Python + 011's libs; records ride the existing `results`/
  Postgres (verify in T254).
- Principle IV completed: monitoring now covers **input *and* output** drift (no stage added/dropped).
- Principle VI advanced: output/ground-truth quality tracking + gauges + stored reports.
- No new runtime/service → **no amendment** (gated vs the constitution as it stands; re-check if a v1.4.0
  line ratifies before implementation — expected still clean).

## Implementation Strategy

1. **Pre-flight** (011 libs + stores), then **log predictions + ingest labels** (US1) behind the 006
   fire-and-forget/fail-open pattern. **Stop and validate** (logging never breaks serving).
2. **Compute windowed quality** (US2) → gauges + reports + extended Grafana panel. Validate against a
   hand-computed value.
3. **Quality breach → existing retrain + combine policy** (US3), each breach signal independently
   observable.
4. **No-regression sweep**: input-PSI path, UI, serving/VRAM mutex, footprint, GPU freeze — never regress.

## Out of Scope (recorded)
- **Replacing input-PSI drift** — 013 complements it; the feature-distribution path stays (Non-Goal).
- **Per-request online quality** — quality is a **windowed** aggregate over labeled pairs, not a synchronous
  per-inference verdict (Non-Goal).
- **Evidently / heavy eval deps** — dependency-light, pure-Python + 011's libs (Principle III) (Non-Goal).
- **A new label datastore / labeling UI** — labels ride the existing stores (Non-Goal).
- **A second resident model for scoring** — CPU-side aggregation; Principle II untouched (Non-Goal).
- **Automated label / weak-supervision generation** — labels come from the operator's ingestion path
  (Non-Goal).
