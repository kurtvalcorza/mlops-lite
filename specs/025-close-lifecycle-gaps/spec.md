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

Batch inference has two correctness gaps. (a) The job layer admits `asr` as a batch modality, but the batch flow has no ASR path and raises `ValueError` at runtime — an accepted job that always fails. (b) The flow scores whichever model version the serving tenant currently *happens* to hold; it does not load/assert the requested `model`/`registry_version` first (the inherited SC-068 gap), so a batch launched while serving holds a different version silently scores the wrong model.

**Why this priority**: These are correctness bugs in a shipped feature — an accepted job that fails, and a silent wrong-answer. Highest value, smallest change.

**Independent Test**: A batch submitted for a version that is not currently resident scores *that* version (asserted), and an ASR batch runs to completion instead of raising. Offline via injected predict_fn for the ordering/assertion logic; the real load-under-lease leg validated on hardware (SC-068 is a hardware SC).

**Acceptance Scenarios**:

1. **Given** the serving tenant holds version B, **When** a batch is launched for version A, **Then** the flow loads/asserts A (under the single-tenant lease) before scoring, or refuses with a clear error — it never silently scores B.
2. **Given** an ASR batch job, **When** it runs, **Then** it produces transcriptions via a real ASR batch path (no `ValueError`), or ASR is removed from the admitted batch modalities so the job is rejected at submission rather than accepted-then-failed.
3. **Given** the version-assertion, **When** validated on the target hardware, **Then** the one-GPU-tenant invariant is preserved (load happens through admission, jobs remain non-preemptable).

---

### User Story 2 - Tabular becomes a full lifecycle modality (Priority: P2)

Tabular serves (LightGBM child, CPU/off-lease) but is a half-modality: there is no fine-tune flow, no held-out eval fixture, its AUC metric is a stub, and it is excluded from quality monitoring. It cannot participate in train→gate→serve→monitor→retrain like vision.

**Why this priority**: Completes Principle IV coverage for a modality the platform already serves. Feature-sized (comparable to a 010 modality slice) but self-contained.

**Independent Test**: A tabular dataset can be fine-tuned into a registered version, scored on a held-out AUC fixture, gate-compared against the incumbent, promoted, served, and — where a per-request ground-truth label exists — quality-monitored, all without a GPU (tabular is CPU/off-lease).

**Acceptance Scenarios**:

1. **Given** a registered tabular dataset, **When** a tabular fine-tune is launched, **Then** it trains a model, registers a version with the tabular task/engine tags and its logged eval metric, and cleans up on failure (no partial version) — mirroring the existing modality flows.
2. **Given** a tabular candidate, **When** it is promoted, **Then** the evaluation gate compares its AUC against the incumbent on a committed held-out fixture (AUC is no longer a stub).
3. **Given** tabular predictions with delayed ground-truth labels, **When** a quality window is scored, **Then** tabular quality is computed and can drive the existing breach→retrain policy (or, if a modality is intentionally excluded from quality, that exclusion is documented with rationale).
4. **Given** the tabular fine-tune, **When** it runs, **Then** it adds no heavy dependency beyond what tabular serving already requires (Principle III) and holds no GPU lease (CPU/off-lease).

---

### User Story 3 - Operator can download dataset bytes from the console (Priority: P3)

Dataset byte-download is browser-unreachable (021 FR-215, deferred): the console can browse dataset versions but an operator cannot download the actual bytes.

**Why this priority**: A real data-stage usability gap, but lower urgency than correctness/capability. Small.

**Independent Test**: From the data page, an operator downloads a dataset version's bytes; the download is proxied/presigned through the BFF so object-store credentials never reach the browser.

**Acceptance Scenarios**:

1. **Given** a registered dataset version, **When** the operator requests download in the console, **Then** the bytes are delivered via the key-injecting BFF (presigned or proxied), with credentials never exposed to the browser.

---

### User Story 4 - Streamed LLM predictions are logged like non-streamed ones (Priority: P3)

Full logging/labeling of streamed LLM predictions is out of scope today (022): predictions served over the SSE streaming path are not captured for quality/shadow the way non-streamed `/infer` predictions are.

**Why this priority**: Closes an observability blind spot for the streaming path, but requires care on the fire-and-forget capture seam. Medium.

**Independent Test**: A prediction served over `/infer/stream` produces the same prediction-log + capture rows (fail-open, off the response path) as a non-streamed prediction, so it can be labeled and enter quality/shadow.

**Acceptance Scenarios**:

1. **Given** a streamed LLM completion, **When** it finishes (or is captured incrementally), **Then** a prediction-log/capture row is written fail-open off the response path, identifiable by prediction id for later label attach — matching the non-streamed contract.

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
- Tabular quality where no clean per-request label exists → document the exclusion rather than fabricate labels.
- Streamed-prediction capture must never block or alter the streamed response → fail-open, off the response path, like the existing tracing/quality capture.
- Any persisted-state change (e.g. a tabular-specific column) → a NEW numbered migration, never an edit to an applied one.
- The parked features (US3–US6) risk sprawling → each is independently shippable; if a story proves larger than a slice, it spins into its own follow-on increment rather than bloating a single PR.

## Requirements *(mandatory)*

### Functional Requirements

**Batch correctness (US1)**

- **FR-001**: Batch inference MUST score the requested `model`/`registry_version` — loading/asserting it before scoring under the single-GPU-tenant lease — or refuse with a clear error; it MUST NOT silently score whatever version is resident (closes SC-068).
- **FR-002**: Every modality admitted as a batch modality MUST have a working batch path; ASR MUST either gain a real batch path or be removed from the admitted set so it is rejected at submission, never accepted-then-failed at runtime.
- **FR-003**: The batch load/assert MUST preserve Principle II — model loads go through admission, running jobs are never preempted.

**Tabular full modality (US2)**

- **FR-004**: A tabular fine-tune flow MUST exist, registering a version with tabular task/engine tags and its logged eval metric, with failure cleanup (no partial version), mirroring the existing modality flows.
- **FR-005**: A committed held-out tabular eval fixture + AUC scorer MUST exist so tabular promotion is gated on a real metric (AUC is no longer a stub).
- **FR-006**: Tabular MUST be integrated into quality monitoring where a per-request ground-truth label is available, so it can drive the existing breach→retrain policy; any intentional exclusion MUST be documented with rationale.
- **FR-007**: Tabular training MUST remain CPU/off-lease and add no heavy dependency beyond tabular serving's existing footprint (Principles II and III).

**Parked operator/data features (US3–US6)**

- **FR-008**: The operator console MUST let an operator download a dataset version's bytes via the key-injecting BFF (presigned or proxied), with object-store credentials never reaching the browser (closes 021 FR-215).
- **FR-009**: Predictions served over the SSE streaming path MUST be captured (prediction-log/capture rows) fail-open and off the response path, identifiable by prediction id, matching the non-streamed contract — so streamed predictions can be labeled and enter quality/shadow.
- **FR-010**: The console MUST surface live per-trial HPO progress (completed trials + objective values, updating live) within the dependency-light, single-machine constraints (no external dashboard service).
- **FR-011**: The console MUST let an operator dispatch shadow-replay and read its advisory verdict using the existing backend endpoints, with verdicts clearly marked advisory (never gating).

**Cross-cutting**

- **FR-012**: Any persisted-schema change MUST land as a NEW numbered `platformlib/migrations/*.sql` file plus a contract update; applied migrations MUST NOT be edited and DDL MUST NOT be inlined in code.
- **FR-013**: No change may add a heavy dependency to the gateway or agent images, or introduce a second concurrent GPU tenant.
- **FR-014**: `docs/current-architecture.md` MUST be updated in the same increment if any Snapshot row (topology, data authority, invariants) changes.

### Key Entities *(capability surfaces, not new data model unless noted)*

- **Batch flow** (`training/flows/batch_infer.py`, `gateway/app/batch.py`): gains version-assertion + an ASR path (or ASR removal from admitted modalities).
- **Tabular training** (new `training/flows/tabular_finetune.py` + `training/scoring` + a `benchmarks/tabular/*` fixture): the missing produce/eval half of the modality.
- **Console surfaces** (`ui/app/data`, `ui/app/monitoring`/serving, `ui/app/training`, `ui/app/models`): dataset download, streamed-prediction visibility, HPO progress, shadow-replay dispatch/verdict.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A batch for a non-resident version scores that exact version (or refuses) — never the resident one — proven offline for the ordering and on hardware for the load-under-lease leg (SC-068 closed).
- **SC-002**: An ASR batch job completes successfully, OR ASR batch submissions are rejected at submission time; no admitted batch modality raises at runtime.
- **SC-003**: A tabular dataset can be fine-tuned → registered-with-metric → gate-compared on a committed AUC fixture → promoted → served, with no GPU lease held and no new heavy dependency.
- **SC-004**: Tabular predictions with labels produce a quality window that can trip the existing breach→retrain policy (or the exclusion is documented).
- **SC-005**: An operator downloads dataset bytes from the console with no object-store credential reaching the browser.
- **SC-006**: A streamed LLM prediction yields the same log/capture rows as a non-streamed one and can be labeled.
- **SC-007**: A running HPO study's trials are visible live in the console with no external dashboard service added.
- **SC-008**: An operator dispatches shadow-replay and reads its advisory verdict entirely from the console.
- **SC-009**: The existing offline suite stays green throughout; every new capability adds tests (web-free where the logic is web-free), and no on-GPU behavior violates the one-tenant lease.

## Assumptions

- **US1 and US2 are the committed core** (P1/P2); **US3–US6 (the parked features) are lower priority and expected to phase into follow-on increments (026+)** under Principle VII — 025's spec scopes them so they are tracked, not lost. If any US3–US6 story proves larger than a slice, it spins into its own feature rather than bloating 025.
- Behavior change is expected and permitted here (unlike 024); each change is explicit, and any external-contract/schema change is gated by FR-012.
- On-hardware validation (the RTX 5070 Ti box) is required to close SC-001's load-under-lease leg and any GPU-touching SC — those cannot be closed from the offline environment alone (constitution "gate zero").
- The git working branch is the designated feature branch; the spec directory (`specs/025-close-lifecycle-gaps`) is the source of truth for downstream `/speckit-plan` and `/speckit-tasks`.
- No change drops or bypasses an existing lifecycle stage, resurrects a retired port/daemon, or weakens the single gated promotion choke-point.
