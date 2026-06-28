# Feature Specification: Model-Quality Monitoring with Ground Truth

**Feature Branch**: `013-quality-monitoring`

**Created**: 2026-06-28

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

**Input**: The monitoring stage today watches only the **input** side: `gateway/app/monitoring.py`
computes pure-Python **PSI** (Population Stability Index) over *feature distributions* between two
dataset versions, exports `gateway_drift_score` / `gateway_dataset_drift`, writes reports to the MinIO
`results` bucket, surfaces them on the Grafana dashboard, and `POST /monitor/check` can close the loop by
firing a retrain on the training daemon. That is **input-distribution drift only**. It cannot see whether
the model's *predictions* are actually getting worse — concept drift lives on the **outputs**, and
catching it requires **ground truth**. 013 adds the missing monitoring half: log served predictions,
attach (usually delayed) labels, compute prediction-**quality** metrics over time windows, surface them,
and let a quality drop *also* trigger a retrain — complementing, not replacing, the input-PSI signal.

> **Scope note**: 013 is a **monitoring extension within the existing stage** (Principle IV, Full
> Lifecycle Coverage — already present). It adds **no new lifecycle stage, no new service, and no new
> runtime**: predictions and labels land in the **existing** MinIO `results` bucket (and/or the existing
> Postgres), quality is computed with the **same** pure-Python style as the PSI module (reusing 011's
> per-modality metric libs), exposed via the **same** Prometheus/Grafana path, and a breach reuses the
> **same** `_launch_retrain` trigger. Requirement IDs continue the shared space (FR-119+, SC-075+, tasks
> T238+). **No constitution amendment** — 013 *advances* Principle VI (Observability) by adding
> output/quality tracking on top of input-drift tracking. See plan.md → Constitution Check.

> **Hard boundary (NON-NEGOTIABLE)**: 013 inherits every standing constraint. The **Blackwell sm_120 GPU
> stack stays frozen** (no torch/transformers movement), **one model in VRAM at a time** (Principle II) is
> untouched — quality computation is CPU-side aggregation off the request path, never a second resident
> model — and **no heavy deps** (Evidently was deliberately removed for Principle III; 013 stays
> dependency-light, reusing the metric libs already in the tree).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Prediction + (delayed) label logging (Priority: P1)

Every served prediction is logged with a stable **prediction id** and the **model version** that produced
it (the registry alias/version resolved at serve time), so it can be correlated later. A separate path
**attaches ground-truth labels** to those predictions whenever they become known — which, for real
concept drift, is **delayed** (minutes to days after serving). Predictions and labels persist in the
existing stores (MinIO `results` / Postgres), keyed by prediction id, ready for windowed quality scoring.

**Why this priority**: Quality-vs-ground-truth is impossible without first capturing *what was predicted*
and *what was correct*. This is the data-capture foundation the other two stories compute on; it leads.

**Independent Test**: Serve N inferences; confirm each produced a logged prediction record carrying a
unique prediction id + the resolved model version. Submit labels for a subset (out of band, later);
confirm the labels attach to the right prediction ids and that un-labeled predictions remain pending (not
dropped, not scored).

**Acceptance Scenarios**:

1. **Given** a served `/infer` (or `/vision/classify`) call, **When** prediction logging is enabled,
   **Then** a prediction record is written with a unique prediction id, the model version, the input
   reference, the prediction, and a timestamp — fire-and-forget, off the request path, fail-open (logging
   failure never breaks serving).
2. **Given** logged predictions, **When** ground-truth labels arrive later via `POST /monitor/labels` (or
   the thin batch client looping it), **Then** each label is matched to its prediction id and stored,
   leaving predictions with no label yet in a clear *pending* state.
3. **Given** a label for a prediction id that does not exist (or already has a label), **When** it is
   ingested, **Then** the system rejects/ignores it cleanly (no corruption, a clear result), never
   silently overwriting served history.

---

### User Story 2 — Windowed quality-metric computation (Priority: P1)

Over a **sliding count-based window** (the last **N** labeled pairs, **N** configurable), recomputed
**on demand** via `POST /monitor/quality/check` (and optionally periodically), the platform joins
predictions to their labels and computes prediction-**quality** metrics **per modality** — reusing 011's
metric libraries (e.g. classification **accuracy** / F1 for vision/classification, **WER** for ASR-style
text, the appropriate metric per task) — then exports the result as **Prometheus gauges** and surfaces it
on an **extended Grafana panel**,
alongside the existing input-PSI panels. A quality report is written to the MinIO `results` bucket in the
same shape/spirit as the drift reports.

**Why this priority**: This is the actual *quality signal*. Without it there is nothing to display and
nothing to threshold on; it is co-P1 with US1 because the two together deliver the monitoring half.

**Independent Test**: With a window of labeled predictions for a known model version, compute the quality
metric, assert it matches a hand-computed expected value, see the gauge update, see the new Grafana panel
render it, and find a quality report object in the `results` bucket.

**Acceptance Scenarios**:

1. **Given** a window of predictions that have labels, **When** quality is computed for a model version,
   **Then** the correct per-modality metric (accuracy/WER/etc.) is produced, only over labeled pairs, and
   un-labeled predictions are excluded (not counted as wrong).
2. **Given** a computed quality value, **When** the gateway is scraped, **Then** a Prometheus gauge
   (labeled by model version / modality) reflects it, and the Grafana dashboard shows it on a panel
   added next to the input-drift panels.
3. **Given** a window with too few labeled pairs to be meaningful, **When** quality is requested, **Then**
   the result is reported as *insufficient data* (not a misleadingly precise number) and no breach fires.

---

### User Story 3 — Quality-breach → retrain trigger (Priority: P2)

When a quality metric **degrades past a threshold** (per modality), the platform triggers the **existing**
retrain path — the same `_launch_retrain` → training-daemon call the input-PSI breach already uses. This
is the **second, complementary** retrain signal: a retrain may be warranted by **input drift** (PSI),
**output-quality drop** (013), or both, and the spec defines how the two combine to gate a retrain.

**Why this priority**: It closes the output-side feedback loop (Principle IV), but depends on US1+US2
existing first, so P2. It reuses the existing trigger; the new work is the threshold + the combine policy.

**Independent Test**: Feed a labeled window whose quality is below threshold; confirm a retrain is launched
on the training daemon (counter increments) with the right spec; confirm a window above threshold launches
nothing; confirm the combine policy with the input-PSI signal behaves as specified (e.g. neither signal
alone vs both).

**Acceptance Scenarios**:

1. **Given** a quality metric below its degradation threshold for a model version, **When** the quality
   check runs (and a retrain spec is supplied), **Then** the existing retrain trigger fires exactly once
   and the retrain counter increments — fail-soft if the trainer is unreachable (the quality report still
   stands).
2. **Given** a quality metric at/above threshold, **When** the check runs, **Then** no retrain is
   launched.
3. **Given** both the input-PSI signal and the output-quality signal, **When** a retrain decision is made,
   **Then** it follows the documented combine policy (**OR + cooldown** — either signal fires, debounced),
   with each signal independently observable so an operator can see *why* a retrain fired.

---

### Edge Cases

- **Delayed / missing labels**: most predictions are scored late or never; quality is computed only over
  the labeled subset within the window. Un-labeled predictions are *pending*, never counted as correct or
  incorrect (FR-119/FR-122).
- **Late-arriving labels**: a label can arrive after its prediction has aged out of the *current* window;
  the join is by prediction id over the stored history, so a late label still updates the window it
  belongs to on the next computation (FR-120).
- **Model-version skew**: predictions are tagged with the model version that served them; a quality drop
  must be attributable to a specific version (so a freshly-promoted model isn't blamed for the old one's
  errors) (FR-121).
- **Thin windows**: too few labeled pairs → *insufficient data*, no false breach (FR-123).
- **Fail-open logging**: a prediction-logging or label-store failure must never break `/infer` or
  `/vision/classify` — logging is off the request path, exactly like 006 tracing (FR-119).
- **Two complementary signals, one loop**: input-PSI breach and output-quality breach must not double-fire
  uncontrolled retrains; the combine policy + the existing one-model-in-VRAM/one-trainer-busy guards bound
  it (FR-125, FR-126).
- **No regression**: the existing input-PSI drift path, its gauges, its `/monitor/check` retrain, the six
  UI tabs, SSE framing, and the VRAM mutex all behave identically (SC-080).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-119**: The gateway MUST log every served prediction (`/infer`, `/infer/stream`, `/vision/classify`)
  as a record carrying a **unique prediction id**, the **model version** resolved at serve time, an input
  reference, the prediction/output (subject to a capture toggle, mirroring `MLFLOW_TRACE_CAPTURE_IO`), and
  a timestamp. Logging MUST be **fire-and-forget, off the request path, and fail-open** — a logging
  failure MUST NOT alter or break the served response (the 006 tracing pattern).
- **FR-120**: The platform MUST provide a **label-ingestion path** whose primary primitive is an **API
  endpoint** — `POST /monitor/labels` (attach a ground-truth label to a `prediction_id`) — that attaches
  ground-truth labels to previously-logged predictions **by prediction id**, supporting **delayed** arrival
  (labels submitted long after serving). A **thin batch client** (mirroring `data/register_dataset.py`)
  loops the endpoint for bulk/delayed labels, covering online + bulk in one path. Predictions remain in a
  **pending** state until labeled; labels are matched by id over the stored history so **late-arriving**
  labels still count.
- **FR-121**: Prediction and label records MUST persist in the **existing** stores (MinIO `results` bucket
  and/or Postgres — no new datastore), keyed so predictions and labels join by prediction id and every
  record is attributable to its **model version**.
- **FR-122**: Over a **sliding count-based window** (the **last N labeled pairs**, with **N configurable**),
  the platform MUST compute prediction-**quality** metrics **per modality**, joining predictions to labels
  and scoring **only labeled pairs** (un-labeled predictions excluded, never counted as wrong). Quality MUST
  be recomputable **on demand** via a `POST /monitor/quality/check` endpoint (mirroring the existing
  `POST /monitor/check` PSI pattern), and MAY also run **periodically**. Count-based sliding windows are
  deterministic and robust to sparse delayed labels (a thin window ⇒ *insufficient data*, never a false
  breach). Metric computation MUST **reuse 011's metric libraries** (e.g. accuracy/F1 for classification,
  WER for ASR text) and stay **dependency-light** (no Evidently / no heavy new deps), matching the
  pure-Python PSI module's footprint.
- **FR-123**: Each computed quality value MUST be exported as a **Prometheus gauge** (labeled by model
  version and/or modality) and a **quality report** written to the MinIO `results` bucket (shape/spirit of
  the existing drift reports). A window with too few labeled pairs MUST report **insufficient data** rather
  than a misleading score, and MUST NOT trigger a breach.
- **FR-124**: The **existing Grafana dashboard** (`infra/grafana/.../mlops-lite.json`) MUST be **extended**
  with a model-quality panel (per modality / model version) placed alongside the existing input-drift
  panels — extending the dashboard, not replacing it.
- **FR-125**: A quality breach is **relative-to-baseline**: when the windowed metric **drops more than a
  configurable X% below the model's registered/eval (011) baseline** metric (per modality), the platform
  MUST trigger the **existing** retrain path (`_launch_retrain` → training daemon), incrementing the
  retrain counter (with a label distinguishing the **quality** trigger from the **input-PSI** trigger).
  The per-modality baseline metrics (accuracy/F1/WER/etc.) **reuse 011's libs** and tie to the **011 eval
  baseline**, accounting for task difficulty; the X% drop is **configurable**. The trigger MUST be
  **fail-soft** (trainer unreachable ⇒ report still stands) and respect the existing one-trainer-busy /
  one-model-in-VRAM guards.
- **FR-126**: The input-PSI breach and the output-quality breach combine under an **OR + cooldown** policy:
  **either** the input-PSI breach **OR** the quality breach triggers `_launch_retrain`, with a
  **configurable debounce/cooldown** window to prevent retrain storms. This catches both the leading
  (input-distribution) and the confirmed (ground-truth quality) signals. Each signal MUST remain
  **independently observable** (separate gauges/labels) so an operator can see which signal fired.
- **FR-127**: 013 MUST NOT regress the existing **input-PSI** drift path (its gauges, reports,
  `/monitor/check` retrain), the six UI tabs, SSE framing, fail-open posture, the loopback/auth/BFF
  contract, or the one-model-in-VRAM mutex. Prediction logging MUST add no second resident model and no
  measurable regression to inference latency or the GPU-lock hold time.

### Key Entities *(include if feature involves data)*

- **PredictionRecord**: a logged served prediction — `prediction_id`, `model_version`, modality, input
  reference, prediction/output (capture-toggled), timestamp. The unit a label attaches to.
- **LabelRecord**: a ground-truth label keyed by `prediction_id` — the (usually delayed) correct answer;
  joins to exactly one PredictionRecord.
- **QualityReport**: the windowed output — model version, modality, metric name + value (or *insufficient
  data*), labeled-pair count, window bounds, breach flag, timestamp; stored in the `results` bucket like a
  drift report.
- **QualitySignal**: the per-modality quality gauge + its degradation threshold — the output-side
  complement to the input-PSI `gateway_drift_score` / `gateway_dataset_drift`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-075**: Served predictions are logged with a unique id + model version, fire-and-forget and
  fail-open; a serving call with prediction-logging *down* still returns the identical response.
- **SC-076**: Delayed ground-truth labels attach to the correct predictions by id (including late arrivals
  after the prediction left the current window); un-labeled predictions stay pending and are never scored.
- **SC-077**: Windowed per-modality quality (accuracy/WER/etc., via 011's libs) matches a hand-computed
  expected value over the labeled pairs, with thin windows reported as *insufficient data*.
- **SC-078**: The quality value appears as a Prometheus gauge (by model version/modality), a quality report
  lands in the MinIO `results` bucket, and the extended Grafana dashboard renders a model-quality panel
  beside the input-drift panels.
- **SC-079**: A below-threshold quality window fires the existing retrain trigger exactly once (counter +1,
  labeled as the quality signal, fail-soft on trainer-down); an above-threshold window fires none; the
  input-PSI and quality signals combine per the documented policy and are independently observable.
- **SC-080**: No regression — the input-PSI drift path, its retrain, the six UI tabs, SSE framing, the
  loopback/auth/BFF contract, and the one-model-in-VRAM mutex are unchanged; no second resident model and
  no measurable inference-latency / GPU-lock regression from logging.

## Assumptions

- **011's metric libs exist and are reusable** — the per-modality scoring (accuracy/F1/WER/…) lives in the
  tree from 011 and is callable as a library; 013 reuses it rather than re-implementing or pulling a heavy
  eval dependency.
- **Ground truth is delayed and partial** — the realistic operating mode is that only a subset of
  predictions ever get labeled, and late. Quality is a windowed estimate over whatever is labeled, not a
  per-request verdict.
- **Existing stores suffice** — MinIO `results` (and/or Postgres) holds predictions + labels; no new
  datastore, matching the PSI module's use of `results`.
- **Single local operator, unchanged posture** — loopback binding, fail-closed gateway auth, the BFF
  contract, and the hybrid-GPU one-model-in-VRAM model all stand; logging is CPU-side and off the request
  path.
- **Complement, don't replace** — input-PSI drift remains exactly as is; 013 is purely additive (the
  output-quality half of monitoring).

## Non-Goals

- **Replacing input-PSI drift** — 013 *complements* it; the feature-distribution PSI path stays.
- **Per-request online quality / a verdict on every inference** — quality is a **windowed** aggregate over
  labeled pairs, not a synchronous score blocking the response.
- **Re-introducing Evidently or any heavy eval/monitoring dependency** — 013 stays dependency-light on the
  pure-Python + 011-metric-lib footprint (Principle III).
- **A new label-management UI/datastore** — labels ride the existing stores; any UI surfacing is a thin
  read over existing panels, not a labeling tool.
- **A second resident model for scoring** — quality is CPU-side aggregation; Principle II is untouched.
- **Automated label generation / weak supervision** — labels come from the operator's ingestion path; 013
  does not synthesize ground truth.

## Grilled decisions (2026-06-28)

Resolved at the grill; 013 is **build-ready**:

- **(a) Label ingestion = API endpoint + thin batch wrapper.** Primary primitive: `POST /monitor/labels`
  (attach a ground-truth label to a `prediction_id`). A thin batch client (mirroring
  `data/register_dataset.py`) loops it for bulk/delayed labels — covers online + bulk in one path
  (FR-120, US1, T241).
- **(b) Windowing = sliding count-based (last N labeled pairs), on-demand recompute + optional periodic,
  configurable N.** Quality aggregates the last N labeled pairs; recompute via `POST /monitor/quality/check`
  (mirroring `POST /monitor/check`), optionally also periodic; N configurable. Deterministic + robust to
  sparse delayed labels (thin window ⇒ *insufficient data*, no false breach) (FR-122, US2, T244).
- **(c) Threshold = relative-to-baseline, configurable.** A quality breach = the windowed metric drops more
  than a configurable **X% below the model's registered/eval (011) baseline** metric; per-modality metrics
  reuse 011's libs (accuracy/F1/WER/etc.). Accounts for task difficulty; ties to the 011 eval baseline
  (FR-125, T243/T247).
- **(d) Combine policy = OR + cooldown, configurable.** Either the input-PSI breach **OR** the quality
  breach triggers `_launch_retrain`, with a configurable debounce/cooldown window to prevent retrain
  storms. Catches both the leading (input-distribution) and the confirmed (ground-truth quality) signals;
  each signal independently observable (FR-126, US3, T249).
