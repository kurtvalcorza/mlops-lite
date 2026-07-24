# Feature Specification: Close Lifecycle Gaps

**Feature Branch**: `025-close-lifecycle-gaps`

**Created**: 2026-07-22

**Status**: Draft

**Input**: User description: "We have to fix and close the gaps found in the full-loop review — batch inference correctness, tabular as a full modality, and the previously-parked operator/data features. (The behavior-preserving items — a PSI offline test and doc reconciliation — go to feature 024; this feature is the net-new capability and behavior-change work.)"

## Context

A full-loop review found the platform's lifecycle is complete and closable, but with real **edges**: batch inference has two correctness gaps, tabular is only a half-modality (serves but cannot train→gate→monitor), and four operator/data capabilities were deliberately parked in earlier increments. Unlike feature 024 (behavior-preserving refactor), **025 intentionally changes behavior and adds capability** — so each change is explicit and, where it touches serving/GPU or persisted state, respects the constitution (one GPU tenant; dependency-light; new schema only via numbered migrations) and carries on-hardware validation where the constitution's phase gate requires it.

Priorities: **US1 (batch correctness) and US2 (tabular) are the committed core.** US3–US6 (the parked features) are lower priority and expected to phase into follow-on increments (026+) under the constitution's no-big-bang rule; this spec establishes their scope so they are tracked, not lost.

## Clarifications

### Session 2026-07-22

- Q: Which gaps to close now? → A: Batch correctness, tabular full modality, and the parked UI/features (the behavior-preserving PSI test + doc reconcile were routed to feature 024).
- Q: How to structure vs 024? → A: Keep 024 behavior-preserving; open this new feature 025 for the behavior-changing fixes and net-new capability.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Batch inference scores the right model, for every admitted modality (Priority: P1)

Batch inference has one correctness gap: the flow scores whichever model version the serving tenant currently *happens* to hold; it does not load/assert the requested `model`/`registry_version` first, so a batch launched while serving holds a different version silently scores the wrong model. This is the *explicit-version-honoring* gap 015 left open — **distinct from 015's SC-068**, which deliberately scoped batch OUT (`015/research.md:70-71`: "batch-inferring a dataset with the `@serving` model is correct production behavior, not a mislabel"). 025 does NOT overturn that: a batch against `@serving` stays correct; the fix only addresses a batch that names a *specific* non-resident version.

ASR is **not** a shipped bug. The submission validator (`hostagent/jobs.py` `BATCH_MODALITIES`) already **excludes** `asr`, so an ASR batch is rejected *at submission* today — never accepted-then-failed at runtime. (`GPU_BATCH_MODALITIES`, the post-validation lease-protection set, does include `asr`, but that set is only consulted for a job that has already passed submission.) Adding a real ASR *batch* path is therefore optional net-new capability, not a correctness fix; if it is not wanted, the status quo — rejected at submission — is already correct and this story is a no-op for ASR.

**Why this priority**: This is a correctness bug in a shipped feature — a silent wrong-answer (a batch scoring the resident version instead of the one it named). Highest value, smallest change.

**Independent Test**: A batch submitted for a version that is not currently resident scores *that* version (asserted) — never the resident one. Offline via injected predict_fn for the ordering/assertion logic; the real load-under-lease leg is a hardware SC. ASR needs no new test: it stays rejected at submission (status quo) unless a real ASR batch path is added as net-new — in which case that path runs to completion.

**Acceptance Scenarios**:

1. **Given** the serving tenant holds version B, **When** a batch is launched for version A, **Then** the flow loads/asserts A (under the single-tenant lease) before scoring, or refuses with a clear error — it never silently scores B.
2. **Given** ASR is not an admitted batch modality (`BATCH_MODALITIES` excludes it), **When** an ASR batch is submitted, **Then** it is rejected *at submission* today (status quo — correct, not accepted-then-failed); adding a real ASR batch path is optional net-new that MUST admit `asr` *and* provide the path together, never admit it without one.
3. **Given** the version-assertion, **When** validated on the target hardware, **Then** the one-GPU-tenant invariant is preserved (load happens through admission, jobs remain non-preemptable).

---

### User Story 2 - Tabular becomes a full lifecycle modality (Priority: P2)

Tabular serves (LightGBM child, CPU/off-lease) but is a half-modality: there is no fine-tune flow, no held-out eval fixture, its AUC metric is a stub, and it is excluded from quality monitoring. It cannot participate in train→gate→serve→monitor→retrain like vision.

**Why this priority**: Completes Principle IV coverage for a modality the platform already serves. Feature-sized (comparable to a 010 modality slice) but self-contained.

**Independent Test**: A tabular dataset can be fine-tuned into a registered version, scored on a held-out AUC fixture, gate-compared against the incumbent, promoted, served, and — where a per-request ground-truth label exists — quality-monitored, all without a GPU (tabular is CPU/off-lease).

**Acceptance Scenarios**:

1. **Given** a registered tabular dataset, **When** a tabular fine-tune is launched, **Then** it trains a model, registers a version with the tabular task/engine tags and its logged eval metric, and cleans up on failure (no partial version) — mirroring the existing modality flows.
2. **Given** a tabular candidate, **When** it is promoted, **Then** the evaluation gate compares its AUC against the incumbent on a committed held-out fixture (AUC is no longer a stub).
3. **Given** tabular predictions with delayed ground-truth labels, **When** a quality window is scored, **Then** tabular quality is computed and drives the existing breach→retrain policy. Individual requests for which no ground-truth label is ever supplied simply fall out of the labeled window; the tabular modality as a whole is NOT excludable from quality by documentation (FR-353/SC-178).
4. **Given** the tabular fine-tune, **When** it runs, **Then** it adds no heavy dependency beyond what tabular serving already requires (Principle III) and holds no GPU lease (CPU/off-lease).

---

### User Story 3 - Operator can download dataset bytes from the console (Priority: P3)

Dataset byte-download is browser-unreachable (021 FR-215, deferred): the console can browse dataset versions but an operator cannot download the actual bytes.

**Why this priority**: A real data-stage usability gap, but lower urgency than correctness/capability. Small.

**Independent Test**: From the data page, an operator downloads a dataset version's bytes; the bytes are **proxied through the BFF** (not a presigned URL, which is signed against the browser-unresolvable internal Garage endpoint) so object-store credentials never reach the browser.

**Acceptance Scenarios**:

1. **Given** a registered dataset version, **When** the operator requests download in the console, **Then** the bytes are **proxied through the key-injecting BFF** (never a bare presigned internal URL), with credentials never exposed to the browser.

---

### User Story 4 - Streamed LLM predictions are logged like non-streamed ones (Priority: P3)

Full logging/labeling of streamed LLM predictions is out of scope today (022): predictions served over the SSE streaming path are not captured for quality/shadow the way non-streamed `/infer` predictions are.

**Why this priority**: Closes an observability blind spot for the streaming path, but requires care on the fire-and-forget capture seam. Medium.

**Independent Test**: A prediction served over `/infer/stream` produces the same prediction-log + capture rows (fail-open, off the response path) as a non-streamed prediction, AND the streamed response surfaces the generated prediction id the label endpoint requires, so it can be labeled and enter quality/shadow.

**Acceptance Scenarios**:

1. **Given** a streamed LLM completion, **When** it finishes (or is captured incrementally), **Then** a prediction-log/capture row is written fail-open off the response path, AND the generated prediction id is delivered to the client (e.g. an initial metadata SSE event) so the prediction can later be labeled — matching the non-streamed contract.

---

### User Story 5 - Live per-trial HPO progress is visible in the console (Priority: P4)

Live per-trial HPO visualization was a documented fast-follow to 012: today an operator cannot watch an Optuna study's trials progress live.

**Why this priority**: Nice-to-have visibility; no correctness impact. Medium, mostly UI + a progress stream.

**Independent Test**: While an HPO study runs, the console shows trials completing with their objective values, updating live, without adding a heavy dependency (no external Optuna dashboard service).

**Acceptance Scenarios**:

1. **Given** a running HPO study, **When** the operator opens its console view, **Then** completed trials and their objective values appear and update live within the dependency-light, single-machine constraints.

---

### User Story 6 - Operator can run and read shadow-replay from the console (Priority: P4)

The shadow-replay *backend* is fully implemented (feature 016) but its console UI was deliberately deferred (021): dispatch is API-only (`POST /models/{name}/shadow-replay`), and verdicts are read via API.

**Why this priority**: Surfaces an existing, tested backend to operators; no new engine work. Medium, mostly UI.

**Independent Test**: From the console, an operator dispatches a shadow-replay for a candidate and reads its advisory verdict, using only the existing backend endpoints.

**Acceptance Scenarios**:

1. **Given** a candidate version, **When** the operator dispatches shadow-replay from the console, **Then** it calls the existing backend and later displays the advisory verdict — clearly marked advisory (never gating).

---

### Edge Cases

- A batch version-assertion would require loading a model while a job holds the GPU → jobs are non-preemptable; the batch must queue/refuse, never preempt (Principle II).
- Tabular quality: an *individual* request with no clean label falls out of the labeled window (never fabricate labels) — the tabular modality as a whole stays a mandatory quality participant, not excludable by documentation (FR-353/SC-178).
- Streamed-prediction capture must never block or alter the streamed response — **except** the FR-356-required initial `prediction_id` metadata event the client needs to attach a label → otherwise fail-open, off the response path, like the existing tracing/quality capture.
- Any persisted-state change (e.g. a tabular-specific column) → a NEW numbered migration, never an edit to an applied one.
- The parked features (US3–US6) risk sprawling → each is independently shippable; if a story proves larger than a slice, it spins into its own follow-on increment rather than bloating a single PR.

## Requirements *(mandatory)*

### Functional Requirements

**Batch correctness (US1)**

- **FR-348**: Batch inference MUST score the requested `model`/`registry_version` — loading/asserting it before scoring under the single-GPU-tenant lease — or refuse with a clear error; it MUST NOT silently score whatever version is resident. (This closes the explicit-`registry_version`-honoring gap batch never got — NOT 015's SC-068, which correctly kept batch-vs-`@serving` scoring as production-correct; a batch that requests `@serving` is unchanged.)
- **FR-349**: Every modality *admitted* as a batch modality (`hostagent/jobs.py` `BATCH_MODALITIES`) MUST have a working batch path. This does NOT hold today for **tabular**: tabular is admitted, but `batch_infer.py` posts `{"features": row}` while the tabular child requires `{"rows": [...]}` (`serving/children/tabular_service.py:99-127`), so every tabular batch row 422s — large batches abort, and small ones can even record `succeeded` with zero outputs. The tabular batch payload MUST be repaired and tested. (`asr`, by contrast, is NOT admitted — rejected at submission — so adding an ASR path is optional net-new: admit `asr` *and* provide the path together, never one without the other.)
- **FR-350**: The batch load/assert MUST preserve Principle II — model loads go through admission, running jobs are never preempted. Two guarantees are required around the temporary target:
  (a) **Restore** — after scoring, the batch MUST restore the prior serving target (or unload the temporary one) in a `finally`, on both success and failure (including a failure of the *load* itself, which may have already disturbed the prior engine), so online `/infer` never inherits the batch's version afterward. The restore MUST **re-read the latest desired target** (a promote can legitimately land mid-batch — `models.py:124-142` moves the pointer while `swap.py:170-171` defers the reload because a GPU batch is active), not blindly restore the captured snapshot, or it would erase the newer promotion; use a generation/CAS or re-read on the desired pointer.
  (b) **Batch-wide exclusion** — restore-after is not enough on its own: online `/infer` shares the *same* resident engine and the runtime mutex is held per-request (not for the whole batch), so an online call arriving *between* batch rows would be served the batch's temporary version. The batch MUST hold a batch-wide exclusion over the shared engine for the temporary target's lifetime — queueing or refusing online inference (not merely blocking eviction via `_gpu_batch_active`). The exclusion MUST let the **batch's own scoring rows through**: `_predict_fn` posts each row to the same `/engines/*/…` HTTP paths online traffic uses, in separate agent threads, so a naive global exclusion would deadlock the batch against itself — the batch's requests need an authenticated marker/token (or a direct agent-internal scoring seam) to bypass. Design tension (Principle II): a *second* engine would violate one-model-in-VRAM, so on single-GPU hardware this exclusion means online inference is briefly unavailable for the batch's duration — an explicit, hardware-confirmed trade-off, not a second tenant.

**Tabular full modality (US2)**

- **FR-351**: A tabular fine-tune flow MUST exist, registering a version with tabular task/engine tags and its logged eval metric, with failure cleanup (no partial version), mirroring the existing modality flows. A promoted tabular version MUST also **go live on an already-warm child** via version-aware invalidation/reload on promote — NOT by relying on idle-release: the tabular child (`serving/children/tabular_service.py:77-86`) re-resolves `@serving` only when `_bundle is None`, and sustained traffic refreshes `_last_used` so the idle-release never fires, leaving production on the old booster after the alias moves.
- **FR-352**: A committed held-out tabular eval fixture + a tabular **prediction factory** (`predict_fn`) MUST exist so the **existing** pure-Python `auc` metric (`evaluation.py:153-173`, already in `METRICS`) can gate tabular promotion — the metric is *promoted from stub*, not re-implemented.
- **FR-353**: Tabular MUST be a full quality participant — prediction logging (per-row ids + the served version), delayed-label scoring, and breach→retrain wiring are all MANDATORY (this is the half-modality US2 exists to close). A documented exclusion is limited to *individual requests for which no ground-truth label is ever supplied* (those rows simply never enter the labeled window); it MUST NOT be used to exclude the tabular modality as a whole from quality monitoring.
- **FR-354**: Tabular training MUST remain CPU/off-lease and add no heavy dependency beyond tabular serving's existing footprint (Principles II and III).

**Parked operator/data features (US3–US6)**

- **FR-355**: The operator console MUST let an operator download a dataset version's bytes by **proxying the bytes through the gateway/BFF** — NOT by handing the browser a presigned URL, which is signed against the internal Garage endpoint (`garage:3900`, `gateway/app/datasets.py:130-134`) and is unresolvable from the browser. Object-store credentials MUST never reach the browser (closes 021 FR-215).
- **FR-356**: Predictions served over the SSE streaming path MUST be captured (prediction-log/capture rows) fail-open and off the response path, matching the non-streamed contract. Because `quality.log_prediction` generates the prediction id internally and the label endpoint requires a caller-supplied id (there is no prediction-list endpoint), the streaming response MUST also **deliver that id to the client** — e.g. an initial metadata SSE event — without otherwise altering the stream; the client-facing test MUST assert the id is received. Otherwise streamed predictions cannot be labeled and SC-180 is unreachable.
- **FR-357**: The console MUST surface live per-trial HPO progress (completed trials + objective values, updating live) within the dependency-light, single-machine constraints (no external dashboard service).
- **FR-358**: The console MUST let an operator dispatch shadow-replay and read its advisory verdict using the existing backend endpoints, with verdicts clearly marked advisory (never gating).

**Cross-cutting**

- **FR-359**: API and schema changes are governed independently (as in 024's FR-344). A **persisted-schema** change MUST land as a NEW numbered `platformlib/migrations/*.sql` file (applied migrations MUST NOT be edited; DDL MUST NOT be inlined in code). An **external gateway/agent API** change MUST land a contract update. A schema-only internal change therefore needs a migration but NO contract update; an API change needs a contract update but no migration.
- **FR-360**: No change may add a heavy dependency to the gateway or agent images, or introduce a second concurrent GPU tenant.
- **FR-361**: `docs/current-architecture.md` MUST be updated in the same increment if any Snapshot row (topology, data authority, invariants) changes.

### Key Entities *(capability surfaces, not new data model unless noted)*

- **Batch flow** (`training/flows/batch_infer.py`, `gateway/app/batch.py`): gains per-modality version-binding, the tabular payload fix, and the batch-wide exclusion. (ASR needs no change — it is not an admitted batch modality; a real ASR batch path is optional net-new only.)
- **Tabular training** (new `training/flows/tabular_finetune.py` + `training/scoring` + a `benchmarks/tabular/*` fixture): the missing produce/eval half of the modality.
- **Console surfaces** (`ui/app/data`, `ui/app/monitoring`/serving, `ui/app/training`, `ui/app/models`): dataset download, streamed-prediction visibility, HPO progress, shadow-replay dispatch/verdict.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-175**: A batch for an *explicitly requested* non-resident version scores that exact version (or refuses) — never the resident one — and after the batch the prior serving target is resident again (verified on hardware after both a successful and a failed batch). Proven offline for the ordering/restore and on hardware for the load-under-lease leg (the explicit-version-honoring gap; 015's SC-068 batch exclusion stays intact).
- **SC-176**: An ASR batch job completes successfully, OR ASR batch submissions are rejected at submission time; no admitted batch modality raises at runtime.
- **SC-177**: A tabular dataset can be fine-tuned → registered-with-metric → gate-compared on a committed AUC fixture → promoted → served, with no GPU lease held and no new heavy dependency. The "served" leg MUST hold under **sustained/warm** traffic — a version promoted while the child is already warm is picked up (warm-v1 → promote-v2 → predict-v2), not only on a cold start.
- **SC-178**: Tabular predictions with labels produce a quality window that trips the existing breach→retrain policy; the modality is NOT excludable from quality by documentation — only individual requests with no supplied label fall out of the window.
- **SC-179**: An operator downloads dataset bytes from the console with no object-store credential reaching the browser.
- **SC-180**: A streamed LLM prediction yields the same log/capture rows as a non-streamed one and can be labeled.
- **SC-181**: A running HPO study's trials are visible live in the console with no external dashboard service added.
- **SC-182**: An operator dispatches shadow-replay and reads its advisory verdict entirely from the console.
- **SC-183**: The existing offline suite stays green throughout; every new capability adds tests (web-free where the logic is web-free), and no on-GPU behavior violates the one-tenant lease.

## Assumptions

- **US1 and US2 are the committed core** (P1/P2); **US3–US6 (the parked features) are lower priority and expected to phase into follow-on increments (026+)** under Principle VII — 025's spec scopes them so they are tracked, not lost. If any US3–US6 story proves larger than a slice, it spins into its own feature rather than bloating 025.
- Behavior change is expected and permitted here (unlike 024); each change is explicit, and any external-contract/schema change is gated by FR-359.
- On-hardware validation (the RTX 5070 Ti box) is required to close SC-175's load-under-lease leg and any GPU-touching SC — those cannot be closed from the offline environment alone (constitution "gate zero").
- The git working branch is the designated feature branch; the spec directory (`specs/025-close-lifecycle-gaps`) is the source of truth for downstream `/speckit-plan` and `/speckit-tasks`.
- No change drops or bypasses an existing lifecycle stage, resurrects a retired port/daemon, or weakens the single gated promotion choke-point.
