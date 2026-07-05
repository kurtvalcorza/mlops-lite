# Feature Specification: 021 Loop-Native Operator Console

**Feature Branch**: `claude/mlops-lite-code-review-vnfhvq`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "Rebuild the operator UI so its information architecture IS the MLOps
lifecycle loop, replacing the flat resource-noun nav (`infer · models · datasets · runs · monitor ·
health`) with an ordered loop the operator reads top-to-bottom."

## Summary

The operator console today is a flat list of resource nouns. It answers "where is the data for X?"
but never shows the **lifecycle loop** the platform exists to run, and three of the eight loop steps
are invisible: promote/gate is buried inside models, retrain is nowhere, and the autonomous
policy/suggestion layer has no UI at all. The single-GPU lease — the platform's defining constraint
— is not surfaced anywhere.

021 rebuilds the console's **information architecture around the loop itself**. The nav bar becomes
the loop, rendered as an ordered cycle: `data → training → models → serving → monitoring →
retraining ⟲`. Off the loop axis sit a persistent **GPU-lease pill** and an enriched **health** tab.
Each stage carries a **live status glyph** so the bar doubles as a whole-loop status board. Every
stage is deepened to expose the capabilities its backing endpoints already support — most notably
the near-invisible monitoring read-side and the entirely-absent autonomous retraining layer.

This is a **front-end information-architecture rebuild**. It adds no gateway, backend, or API
surface; its only non-UI change is extending the browser-facing proxy allow-list so the new views
can reach endpoints that already exist. Principle II is untouched: the UI **visualizes** lease state
and offers only the already-sanctioned operator-confirmed preemptive swap; it never alters admission
semantics.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The nav bar IS the loop (Priority: P1)

An operator opens the console and immediately sees the MLOps lifecycle spelled out in order —
`data → training → models → serving → monitoring → retraining ⟲` — with a loop-back to `data`, plus
an always-visible indicator of what is on the GPU right now. Each stage shows a live glyph
summarizing its state, so "where is the platform in the loop" is legible without opening a tab.

**Why this priority**: This is the feature's core intent — making the lifecycle clear. It reframes
the entire console even before any single tab is deepened, and it is the MVP: shipping only this
already replaces the noun-soup nav with a loop the operator can read.

**Independent Test**: Load the console with the platform running; confirm the nav renders six ordered
loop stages with connectors and a loop-back glyph, an off-axis GPU pill and health entry, per-stage
live badges that update as platform state changes, and that the default landing is the serving
stage.

**Acceptance Scenarios**:

1. **Given** the platform is running, **When** the operator loads the console, **Then** the nav
   renders the six stages in loop order with directional connectors and a loop-back marker, and the
   first view shown is `serving`.
2. **Given** a training run is active and two promotion suggestions are open, **When** the operator
   looks at the nav without opening any tab, **Then** the `training` badge shows an active-run
   indicator and the `retraining` badge shows an open-suggestion count of 2.
3. **Given** a model is resident on the GPU, **When** the operator looks at the header, **Then** the
   GPU pill shows the lease holder, the resident model name, and the swap/idle state; **and** a swap
   in progress is reflected there without navigating to `serving`.
4. **Given** the platform is unreachable, **When** the console loads, **Then** the loop bar still
   renders (stages are navigable) and each live glyph degrades to an unknown/at-rest state rather
   than blocking the page.

---

### User Story 2 - Serving as the multi-engine surface under one lease (Priority: P1)

An operator uses the `serving` stage to run inference across every promoted engine, to see exactly
what is resident on the single GPU and what is contending for it, and — deliberately, with
confirmation — to preempt a resident serving model. Every inference is visibly traceable to the
registry version that answered and to the prediction record it created for monitoring.

**Why this priority**: Serving is the most-used operational surface and the default landing. It is
where the single-GPU lease (Principle II) becomes tangible, and where the serving→monitoring seam
originates. High daily value; independently demonstrable.

**Independent Test**: With several tasks promoted, open `serving`; confirm one panel renders per
promoted task, each engine can be exercised, the lease view shows holder/resident/version and
distinguishes lease-tenant from off-lease engines, preempt requires confirmation, and an LLM
trace-mode response reports its registry version and prediction id.

**Acceptance Scenarios**:

1. **Given** promoted models for several tasks, **When** the operator opens `serving`, **Then** one
   panel renders per promoted task, and a serving version with no task tag renders a read-only
   "no renderer" placeholder rather than an error.
2. **Given** the operator submits an LLM prompt in **stream mode**, **When** the response streams,
   **Then** the panel shows the completion, the resolved registry version that served it, and the
   cold-start load time (no prediction id — streams are not champion-scorable). **And given** the
   operator submits in **trace mode** (`POST /infer`), **Then** the panel additionally shows the
   created prediction id, presented as feeding monitoring with a "label this prediction" hand-off.
3. **Given** a model is resident and the operator requests a preemptive swap, **When** they trigger
   preempt, **Then** the UI first shows a confirmation naming the current holder to be evicted, and
   only proceeds on explicit confirmation.
4. **Given** the lease view is open, **When** the operator inspects it, **Then** lease-tenant engines
   (LLM, vision, ASR, training) are visually distinguished from off-lease engines (tabular, embed),
   and the live per-task list is shown.
5. **Given** batch inference, **When** the operator launches a batch job over a pinned dataset
   version from `serving`, **Then** they can poll it to a result link within the same stage.

---

### User Story 3 - Close the loop: the monitoring read-side (Priority: P2)

An operator uses `monitoring` to see drift and quality history (not just fire a check), to attach
delayed ground-truth labels to served predictions, and — when a check breaches — to trigger a
one-shot retrain distinct from any standing policy. The shared retrain cooldown is visible as a
first-class outcome.

**Why this priority**: This is the largest gap in the current UI, which is nearly write-only here.
Without the read-side and labeling, the loop cannot be closed from the console.

**Independent Test**: Run drift and quality checks; confirm their histories render, a ground-truth
label can be attached to a prediction by id, a breaching check can carry an auto-filled one-shot
retrain, and a debounced retrain surfaces as "skipped: cooldown" rather than an error.

**Acceptance Scenarios**:

1. **Given** prior drift and quality checks, **When** the operator opens `monitoring`, **Then**
   recent drift reports and recent quality reports both render, newest first.
2. **Given** a served prediction, **When** the operator attaches a ground-truth label by prediction
   id (from the labels panel or via the deep-link from `serving`), **Then** the label is recorded and
   a late/duplicate/unknown id is reported cleanly rather than overwriting served history.
3. **Given** a drift or quality check that breaches, **When** the operator has enabled the one-shot
   "retrain if this breaches" option, **Then** the retrain spec is pre-filled from the breached model
   (latest data, prefilled modality/output, defaulted knobs) and requires confirmation.
4. **Given** a retrain fired too recently, **When** a new breach would retrain, **Then** the UI shows
   a "skipped: cooldown" outcome as an expected state, not a failure.

---

### User Story 4 - The autonomous retraining layer, made visible (Priority: P2)

An operator uses `retraining` to declare per-model standing policies, to see whether the loop is
actually turning (last check / next due / pending retrain per model), and to review the suggestions
the scheduler produces — accepting (through the same promotion gate as `models`) or dismissing them.
Enabling hands-off auto-promotion is a deliberate, warned opt-in.

**Why this priority**: Step 8 — the close-the-loop automation — has no UI today. Surfacing it turns
the platform's autonomy from invisible to operable.

**Independent Test**: Declare a policy, observe its cycle status, and act on a suggestion; confirm
auto-promote is off by default and gated behind an explicit warning, and that accepting a
gate-blocked suggestion routes the operator to the override flow rather than bypassing the gate.

**Acceptance Scenarios**:

1. **Given** no policy for a model, **When** the operator declares one, **Then** it is authored via a
   form or an equivalent document view, an invalid declaration is rejected with the field-level
   reasons shown and is not stored, and a valid one is saved.
2. **Given** policied models, **When** the operator views the cycle board, **Then** each model's last
   check, next due, and pending-retrain state are shown.
3. **Given** an open suggestion, **When** the operator accepts it, **Then** the candidate is promoted
   through the same gate as a manual promotion; **and** if the gate blocks it, the suggestion stays
   open and the UI offers a route to the deliberate override flow (override is not available on
   accept).
4. **Given** the auto-promote setting, **When** the operator enables it, **Then** they must pass an
   explicit confirmation warning that the platform will move the live serving pointer without a
   human, and it is off by default.

---

### User Story 5 - Models registry with the promote gate as centerpiece (Priority: P3)

An operator uses `models` to browse versions with clickable lineage back to their run and dataset,
to read a version's evaluation score on demand, to compare a challenger against the serving
champion, and to promote through the evaluation gate — previewing the score first, and overriding a
hard-gate block only through a deliberate, reasoned confirmation.

**Why this priority**: The registry and promote act already exist in the current UI; 021 re-centers
them on the gate decision and adds lineage legibility. Valuable but not blocking the loop reframe.

**Independent Test**: Browse a model's versions; confirm the serving champion is marked, lineage
tags link back to training and data, evaluate returns a score without moving the alias, and promote
runs the gate with override gated behind a typed reason.

**Acceptance Scenarios**:

1. **Given** a model with several versions, **When** the operator browses it, **Then** the current
   serving version is marked and each version's lineage (originating run, dataset version, base
   model, parent) is shown and navigable.
2. **Given** a candidate version, **When** the operator previews it via evaluate, **Then** its score
   is shown without moving the serving pointer.
3. **Given** a candidate that fails the hard gate, **When** the operator promotes it, **Then** the
   serving pointer does not move and the block verdict is shown; **and** overriding requires a
   confirmation that captures a typed reason.
4. **Given** a seeded version with no originating run, **When** the operator browses it, **Then** it
   is visibly distinguished from a trained version (no run lineage).

---

### User Story 6 - Data and training as the loop's entry (Priority: P3)

An operator uses `data` to register, inspect, and validate immutable dataset versions and
to hand a pinned version directly to training; and uses `training` to launch fine-tune runs and HPO
studies with a fixed modality picker whose knobs, default base, editability, and chaining rules
match each modality — with launch aware of the GPU lease and a hand-off to the resulting registered
version.

**Why this priority**: Both tabs already function; 021 adds the manifest-inspect and validate-as-gate
surfaces, the loop hand-offs, and honest modality affordances. Enrichment, not a new capability.

**Independent Test**: Register and validate a dataset version, then jump to training with it
pre-filled; launch a run and confirm the modality picker locks the vision architecture, gates the
chain field to the chainable modalities, and surfaces a VRAM/busy refusal as a first-class outcome.

**Acceptance Scenarios**:

1. **Given** a dataset version, **When** the operator inspects it, **Then** the full manifest is
   shown (byte download deferred — the presigned URL targets the internal store, FR-215), and
   validation renders a readiness report with gate vs. warn dispositions.
2. **Given** a validated version, **When** the operator chooses "train on this version", **Then**
   `training` opens with that dataset version pre-filled.
3. **Given** the training launcher, **When** the operator selects a modality, **Then** only that
   modality's knobs and its pinned default base are shown, the base is read-only for the locked
   modality, and the chain-from-parent field is available only for the chainable modalities.
4. **Given** a resident serving model, **When** the operator launches a run that cannot be admitted,
   **Then** the busy/over-budget refusal is surfaced as a clear first-class outcome, not a generic
   error.

---

### User Story 7 - Health enriched with per-engine liveness (Priority: P3)

An operator uses `health` to see platform liveness alongside a per-engine liveness dot for each
serving subsystem, so a single dead engine is visible without exercising it.

**Why this priority**: A small ops-visibility improvement; lowest priority and independently
shippable.

**Independent Test**: Open `health`; confirm platform liveness plus a per-engine probe dot for each
serving subsystem render, and a down engine shows a distinct state.

**Acceptance Scenarios**:

1. **Given** the platform is up but one engine is down, **When** the operator opens `health`, **Then**
   overall platform liveness is shown and the down engine's probe dot is distinctly not-ok.

---

### Edge Cases

- **Untagged serving version**: a promoted version with no task tag renders a read-only "no renderer"
  placeholder in `serving`, never an error.
- **Cooldown debounce**: a breach whose retrain is suppressed by the shared cooldown surfaces as
  "skipped: cooldown"; a manual one-shot retrain can be debounced by a recent scheduled retrain and
  vice versa — both are shown, not hidden.
- **Gate block on accept**: accepting a suggestion whose candidate fails the gate leaves the
  suggestion open and routes to the override flow; accept never bypasses the gate.
- **Lease refusal on launch**: a run/study launch that is refused because a tenant holds the lease,
  or because it exceeds the VRAM budget, is shown as a distinct first-class outcome.
- **Backing-store outage**: registry/monitor/policy store outages render as a clear degraded state in
  the affected view rather than a blank or broken page.
- **Empty quality window**: a quality check with no labeled pairs in the window steers the operator to
  attach labels first rather than reporting a meaningless score.
- **Concurrent suggestion resolution**: two operators (or a scheduler auto-promote) resolving the same
  suggestion converge on the true final state without a misleading error.
- **Large dataset upload**: registering a very large dataset is a known limitation of the existing
  in-body upload path; the UI does not pretend otherwise (out of scope to change the transport).

## Requirements *(mandatory)*

### Functional Requirements

**Loop-native shell & chrome**

- **FR-208**: The console navigation MUST render the lifecycle as an ordered loop —
  `data → training → models → serving → monitoring → retraining` — with directional connectors and a
  loop-back marker returning to the start, replacing the current flat resource-noun nav.
- **FR-209**: The navigation MUST place `health` and a persistent GPU-lease indicator off the loop
  axis (visually separate from the six ordered stages).
- **FR-210**: Each loop stage MUST display a live status glyph summarizing its most operationally
  relevant state (at minimum: an active-run indicator for training, a candidate-awaiting-promotion
  indicator for models, the resident engine for serving, a breach indicator for monitoring, and an
  open-suggestion count for retraining), updating from the platform's live event stream and light
  polling.
- **FR-211**: The GPU-lease pill MUST be visible on every view and show the current lease holder, the
  resident model name, and the swap/idle state, and MUST link to the serving stage's full lease view.
- **FR-212**: The console MUST default to opening the `serving` stage; there MUST NOT be a separate
  heavy overview landing page (the live nav badges are the loop status board).
- **FR-213**: When the platform is unreachable, the shell MUST still render and navigate; live glyphs
  and the GPU pill MUST degrade to an unknown/at-rest state without blocking the page.

**Data stage**

- **FR-214**: `data` MUST let the operator register a dataset version, list datasets, and browse a
  dataset's versions, indicating when identical content dedupes to an existing version.
- **FR-215**: `data` MUST let the operator inspect a pinned version's full manifest. Downloading the
  pinned bytes is OUT OF SCOPE for 021: the version's `download_url` is presigned against the internal
  object-store endpoint (not browser-reachable, and SigV4 covers the Host header so the BFF cannot
  rewrite it) — a browser download needs a public-presign backend change, which is deferred (would
  break the no-backend-change guarantee; belongs in a later feature).
- **FR-216**: `data` MUST present dataset validation as an explicit pre-train readiness report,
  distinguishing gate failures (blocking) from warnings (advisory).
- **FR-217**: `data` MUST provide a "train on this version" hand-off that opens `training` with the
  pinned dataset version pre-filled.
- **FR-218**: `data` MUST NOT offer edit or delete of versions (immutable, content-addressed) and MUST
  NOT present in-browser data exploration beyond the validation report.

**Training stage**

- **FR-219**: `training` MUST let the operator launch a fine-tune run and an HPO study against a
  pinned dataset version.
- **FR-220**: The modality selector MUST be a fixed set of the supported trainable modalities; on
  selection the UI MUST show only that modality's knobs and its pinned default base model, MUST make
  the base read-only for the modality whose serving architecture is locked, and MUST enable the
  chain-from-parent field only for the modalities that register a re-trainable checkpoint.
- **FR-221**: `training` MUST stream a live run log and expose polled run detail (status + metrics),
  and MUST provide a hand-off to the registered model version once a run completes.
- **FR-222**: `training` MUST make the launcher lease-aware — surfacing a trainer-busy or
  over-VRAM-budget refusal as a distinct first-class outcome — and SHOULD indicate current GPU
  residency before launch.
- **FR-223**: `training` MUST NOT imply capabilities the backend does not support: no run
  cancellation, no interactive model registration, and no persistent run/study history list (the last
  is a documented backend gap, out of scope here).

**Models stage**

- **FR-224**: `models` MUST list models with the current serving champion marked, and MUST let the
  operator browse a model's versions.
- **FR-225**: `models` MUST show each version's lineage (originating run, dataset version, base model,
  parent) as navigable links back to `training` and `data`, and MUST visually distinguish a version
  with no originating run (e.g. a seeded baseline) from a trained one.
- **FR-226**: `models` MUST expose a version's evaluation score on demand (an explicit action, not a
  columnar field), and MUST let the operator compare a challenger against the serving champion.
- **FR-227**: Evaluation MUST default to the modality's benchmark and metric with an advanced
  disclosure to override the benchmark and metric.
- **FR-228**: Promotion MUST follow a preview-then-promote flow: the operator can preview the score
  without moving the serving pointer, then promote through the evaluation gate; a hard-gate block MUST
  leave the serving pointer unchanged and show the block verdict.
- **FR-229**: Overriding a hard-gate block (promote-with-regression) MUST require a confirmation that
  captures a typed reason.
- **FR-230**: `models` MUST NOT offer version deletion, interactive model registration/upload (see
  Assumptions — deferred to feature 022), or a shadow-replay surface (deferred).

**Serving stage**

- **FR-231**: `serving` MUST render one panel per promoted task, discovered dynamically, and MUST show
  a read-only placeholder for a promoted version that carries no task renderer.
- **FR-232**: `serving` MUST make every promoted engine usable — LLM, vision classification, tabular
  prediction, embeddings, and speech transcription — using each engine's existing request shape. The
  LLM panel MUST offer both a **stream mode** (interactive completion over `POST /infer/stream`) and a
  **trace mode** (single-shot `POST /infer`); the latter is the traceable, prediction-logging path.
- **FR-233**: In LLM **trace mode** (`POST /infer`) the response MUST report the resolved registry
  version that served it, the created prediction id, and the cold-start load time, and MUST present
  the prediction id as feeding monitoring. **Stream mode** MUST report the resolved registry version
  (from `serving/state`) and the load time, but does NOT surface a prediction id — the streamed path
  logs the prediction with no id returned and no input capture (016 decision: streamed predictions are
  champion-unscorable), so the monitoring hand-off is offered only from trace mode.
- **FR-234**: `serving` MUST provide a full lease view (holder, resident model, serving version, and
  the live per-task list) that visually distinguishes lease-tenant engines from off-lease engines.
- **FR-235**: A manual preemptive swap MUST be gated behind a confirmation that names the holder to be
  evicted; the UI MUST NOT otherwise alter admission or lease semantics.
- **FR-236**: `serving` MUST host offline batch inference — launching a batch job over a pinned
  dataset version and polling it to a result link.
- **FR-237**: `serving` MUST provide a "label this prediction" hand-off into the monitoring labels
  surface.

**Monitoring stage**

- **FR-238**: `monitoring` MUST let the operator run an input-drift check and an output-quality check,
  and MUST render the history of both (newest first).
- **FR-239**: `monitoring` MUST let the operator attach a ground-truth label to a served prediction by
  prediction id — via a standalone labels panel and via the hand-off from `serving` — handling late,
  duplicate, and unknown ids cleanly without overwriting served history.
- **FR-240**: A manual check MAY carry an optional one-shot "retrain if this breaches" trigger,
  clearly labeled as distinct from standing policies; when enabled, the retrain spec MUST be
  auto-filled from the breached model (latest data, prefilled modality/output, defaulted knobs) and
  MUST require confirmation.
- **FR-241**: The quality baseline MUST auto-resolve with an advanced disclosure to override the
  baseline, window size, and drop threshold.
- **FR-242**: A retrain suppressed by the shared cooldown MUST be shown as a first-class
  "skipped: cooldown" outcome, not an error, in both the manual and scheduled paths.

**Retraining stage**

- **FR-243**: `retraining` MUST let the operator declare, view, update, and delete a per-model
  standing policy; an invalid declaration MUST be rejected with field-level reasons shown and MUST NOT
  be stored.
- **FR-244**: The policy MUST be authorable both as a structured form and as an equivalent document
  view over the same policy, kept consistent with the validation contract.
- **FR-245**: The auto-promote setting (promoting a passing candidate to live serving with no human)
  MUST be off by default and MUST require an explicit confirmation warning before it can be enabled.
- **FR-246**: `retraining` MUST show a per-model cycle board surfacing each policied model's last
  check, next due, and pending-retrain state.
- **FR-247**: `retraining` MUST present the suggestions inbox filterable by state (open / accepted /
  dismissed); accepting a suggestion MUST route through the same promotion gate as a manual promote,
  and a gate-blocked accept MUST leave the suggestion open and route to the deliberate override flow
  (override is not offered on accept).
- **FR-248**: The console MUST make explicit that monitoring performs manual, one-shot checks while
  retraining performs the same checks on a standing schedule — same checks, same gate, same cooldown —
  so the two stages are not read as duplicates.

**Health & liveness**

- **FR-249**: `health` MUST show overall platform liveness plus a per-engine liveness indicator for
  each serving subsystem, with a down engine shown in a distinct not-ok state.

**Cross-cutting**

- **FR-250**: Every high-trust or lease-affecting action MUST require deliberate friction — at minimum
  promote-override (typed reason), preemptive swap (confirmation), and enabling auto-promote (warned
  opt-in).
- **FR-251**: All gateway access MUST continue to route through the existing browser-facing proxy with
  its server-side operator key; any newly surfaced call MUST be added to the proxy allow-list on
  purpose, and no view may call a gateway path/method that is not allow-listed.
- **FR-252**: The rebuild MUST introduce no gateway, backend, or API changes; its only non-UI change is
  extending the proxy allow-list to already-existing endpoints.
- **FR-253**: The console MUST preserve the existing visual design language (monospace aesthetic and
  existing UI primitives); 021 is an information-architecture change, not a visual reskin.

### Key Entities *(UI-facing; all already exist in the platform)*

- **Loop stage**: one of the six ordered lifecycle steps rendered in the nav, each with a live status
  glyph and a detail view.
- **Lease state**: the current GPU holder, resident model, serving version, and swap/idle status —
  surfaced by the GPU pill and the serving lease view.
- **Dataset version**: an immutable, content-addressed dataset with a manifest and a validation
  report.
- **Run / study**: an async training job producing a registered model version.
- **Model version**: a registry entry with lineage tags and a serving-champion flag; carries an
  on-demand evaluation score.
- **Serving task**: a dynamically discovered per-task serving surface.
- **Prediction / label**: a served inference record and its (possibly delayed) ground-truth label.
- **Drift / quality report**: a monitoring check outcome, with a breach flag and optional retrain
  result.
- **Policy**: a per-model standing retrain configuration, including the auto-promote setting and cycle
  status.
- **Suggestion**: a scheduler-produced promotion proposal in an open/accepted/dismissed state.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-134**: On first load, an operator can identify the current position in the lifecycle loop and
  what is resident on the GPU without opening any tab or scrolling.
- **SC-135**: All eight loop steps (register data, train, register model, gate/promote, serve,
  monitor, retrain, close-the-loop) are reachable from the nav and each shows live state — up from
  five reachable and three invisible today.
- **SC-136**: An operator can view drift history, view quality history, attach a ground-truth label,
  and read a quality result entirely from the console — none of which is possible in the current UI.
- **SC-137**: An operator can declare a retrain policy, see whether the loop is turning for each
  policied model, and accept or dismiss a suggestion from the console — a surface that does not exist
  today.
- **SC-138**: Every promoted engine (LLM, vision, tabular, embeddings, speech) is exercisable from the
  serving stage. The engines that log predictions — LLM trace mode (`POST /infer`), vision, and
  transcribe — additionally surface the registry version that served it, the created prediction id,
  and the monitoring hand-off. Embeddings and tabular are exercisable but log no prediction (no id);
  LLM stream mode surfaces the registry version + load time but no id.
- **SC-139**: Every high-trust action (promote-override, preemptive swap, enabling auto-promote)
  requires an explicit confirmation step and cannot be completed with a single unguarded click.
- **SC-140**: 100% of the console's gateway calls resolve through the allow-listed proxy; no view
  issues a non-allow-listed path/method.
- **SC-141**: The rebuild changes no gateway/backend/API code; the platform's automated backend test
  suite passes unchanged, and the only non-UI diff is the proxy allow-list.
- **SC-142**: The serving stage renders correctly for a promoted task that has no dedicated renderer
  (read-only placeholder) and for a platform-unreachable state (degraded, non-blocking), with no blank
  or broken page.

## Assumptions

- **Single operator**: the console remains a single-operator, local-first surface (127.0.0.1); no
  multi-user auth or concurrent-editing model is introduced. Whole-document policy edits and
  suggestion resolution assume this.
- **Backend is frozen for 021**: every capability maps to an endpoint that already exists; the feature
  adds no gateway/backend/API surface. The only non-UI change is extending the browser-facing proxy
  allow-list to the following already-existing endpoints: the dataset-version detail read, the run
  detail read, the single-shot `POST /infer` (LLM trace mode — already returns registry version +
  prediction id + load time and feeds capture/quality), the drift-history read, the quality-check and
  quality-history endpoints, the label submission, and the six per-engine health probes. All
  promote/evaluate/compare, policy, suggestion, and serving-engine routes are already allow-listed
  (policy/suggestion routes are only re-grouped under the retraining stage).
- **Principle II is visualize-only**: the UI surfaces lease state and offers only the
  already-sanctioned operator-confirmed preemptive swap; it never changes admission, lifecycle, or
  swap semantics. A running training/HPO/batch job is never presented as preemptable.
- **Deferred / out of scope** (explicitly not in 021): a shadow-replay UI; interactive model
  registration/upload from the console — this becomes **feature 022, "external / pretrained model
  import" (bring-your-own-model)**, which requires a new upload endpoint and per-modality packaging;
  **browser download of pinned dataset bytes** (FR-215 — the `download_url` is presigned against the
  internal object-store endpoint and is not browser-reachable; a public-presign backend change is
  deferred); a persistent run/study history list (needs a new backend list endpoint); and a columnar
  per-version metrics view (needs metrics folded into the version listing).
- **No new architectures/modalities**: the fixed modality set and the locked serving architecture are
  reflected, not extended.
- **Design continuity**: the existing monospace design language and UI primitives are reused; no
  visual reskin is in scope.
- **Live state source**: per-stage glyphs and the GPU pill are driven by the platform's existing live
  event stream plus light polling; when unavailable they degrade gracefully.
