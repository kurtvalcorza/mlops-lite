# Feature Specification: Registry-Driven LLM Serving & Operator Model Selection

**Feature Branch**: `022-registry-driven-llm-serving`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "Fix the LLM serving quirks surfaced while driving the 021 console end-to-end: the served LLM is env-fixed (promoting in the UI does nothing), fine-tunes register without a task tag (show as 'no renderer'), serving a fine-tune needs base+LoRA which the platform couldn't express, and the served-model label diverges from what actually runs (corrupting monitoring). Make the LLM behave like every other engine and make it operable from the console."

## Overview

Across the platform, four serving engines — vision, embeddings, ASR, tabular — resolve *what they serve* from the model registry: an operator promotes a version to the `@serving` alias in the console and it becomes the live model. The **text-generation (LLM) engine is the sole exception**: it loads a fixed model artifact chosen by host-level configuration and ignores the registry. As a result, promoting an LLM version in the console has no effect, changing the served LLM requires host access (edit a config file, restart the host process), fine-tuned LLMs are neither discoverable nor selectable, and the model name attached to served predictions can disagree with the model that actually produced them — silently corrupting the quality-monitoring signal.

This feature closes that gap: the LLM becomes a registry-driven, console-operable engine like the others. Promoting a text-generation version — a full base model **or** a LoRA fine-tune (adapter on its base) — makes it serve, under the platform's single-GPU lease, with the served identity reported honestly everywhere it appears.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Promote an LLM and it actually serves (Priority: P1)

An operator has a registered text-generation version they want live. From the console they promote it to serving; the platform reloads the LLM to that version, and the next inference is produced by it — with no host access, config edit, or manual restart. Promoting a different version switches it back.

**Why this priority**: This is the entire point — it makes the operator's mental model ("promote a version → it serves") true for the LLM, matching every other engine, and removes the SSH-level workaround that changing the served LLM requires today. Shipping only this already delivers the core value.

**Independent Test**: Promote text-generation version A → inference answers as A. Promote version B → inference answers as B. Both done entirely from the console, verified by the observable difference in inference output, with no host-level action.

**Acceptance Scenarios**:

1. **Given** two registered text-generation versions and the platform idle, **When** the operator promotes version A from the console, **Then** the next inference is produced by version A and the console shows A as the serving LLM.
2. **Given** version A is serving, **When** the operator promotes version B, **Then** the live LLM changes to B (a controlled reload) and subsequent inference is produced by B.
3. **Given** a promotion is issued, **When** it completes, **Then** no host configuration file was edited and no process was manually restarted.

---

### User Story 2 - Honest served-model identity (Priority: P1)

Everywhere the platform names the served LLM — the console's lease/GPU indicator, the serving view, and the record attached to each served prediction — it names the model **and version that actually produced the output**, not a stale configuration label. Because monitoring scores a model by its recorded identity, this keeps the quality signal attributable to the right model.

**Why this priority**: Without honest identity, US1's served fine-tune is mislabeled and its monitoring window is corrupted (predictions attributed to the wrong model+version). Identity coherence is the correctness backbone that makes serving a non-default LLM safe to operate and observe.

**Independent Test**: Serve a non-default LLM version, run several inferences, and confirm the serving-status indicator, the inference response identity, and each recorded prediction all name that exact model+version — and that a quality check for that model+version scores those predictions.

**Acceptance Scenarios**:

1. **Given** a non-default text-generation version is serving, **When** the operator views serving status, **Then** it names that model and version (not a fixed default label).
2. **Given** inferences are served by that version, **When** each prediction is recorded, **Then** it is attributed to that model+version, and a quality check for that model+version includes those predictions.
3. **Given** the served LLM is switched, **When** identity is read again, **Then** it reflects the new model+version within the platform's live-status refresh cadence.

---

### User Story 3 - Serve LoRA fine-tunes (base + adapter) (Priority: P2)

A LoRA fine-tune's registered artifact is an adapter, not a standalone model. When the operator promotes such a fine-tune, the platform serves the adapter applied on its base, so inference reflects the fine-tuned behavior. Promoting back to a full base model restores base behavior.

**Why this priority**: This is what makes fine-tuning *useful* end-to-end — a fine-tune the operator trained can actually go live. It depends on US1/US2 being in place (a promoted LLM serves, with honest identity) and generalizes the existing adapter-serving prototype into a registry-resolved capability.

**Independent Test**: Promote a LoRA fine-tune whose training instilled a distinguishing behavior → that behavior is observable in served inference. Promote back to the base → the behavior is gone. No operator hand-entry of the base at serve time.

**Acceptance Scenarios**:

1. **Given** a registered LoRA fine-tune with a recorded base, **When** the operator promotes it, **Then** the platform resolves the base automatically and serves base+adapter, and inference shows the fine-tuned behavior.
2. **Given** a fine-tune is serving, **When** the operator promotes a full base version, **Then** the adapter is dropped and base behavior serves.
3. **Given** a fine-tune whose base cannot be resolved or is unavailable, **When** the operator promotes it, **Then** the promotion is refused with a clear reason and the currently-served LLM is unchanged.

---

### User Story 4 - Fine-tunes are first-class serving targets (Priority: P2)

A newly-registered LLM fine-tune appears in the console as a real, selectable text-generation serving target with its lineage (base, parent, dataset) — never as an unusable "no renderer" placeholder. Text-generation versions already in the registry that were recorded without those descriptors are still selectable.

**Why this priority**: A fine-tune the operator can't see or select can't be promoted, so US1–US3 are unreachable for it through the console. This makes every valid LLM version operable, including ones already registered.

**Independent Test**: Register a text-generation fine-tune → it lists as a promotable LLM version with lineage and, once serving, renders a working inference panel. A previously-registered untagged LLM version is likewise selectable.

**Acceptance Scenarios**:

1. **Given** a fine-tune is registered, **When** the operator opens the models view, **Then** it appears as a text-generation version with base/parent/dataset lineage and can be promoted.
2. **Given** a promoted LLM version, **When** the operator opens the serving view, **Then** it renders a working inference panel (not a read-only placeholder).
3. **Given** a valid text-generation version registered without task descriptors, **When** the operator views it, **Then** it is still recognized as an LLM version and is selectable.

---

### User Story 5 - Safe switch under the single-GPU lease (Priority: P3)

Switching the served LLM while another model is resident follows the platform's one-tenant-in-VRAM rule: a controlled sequential reload (evict → free → load), never two models at once. If the switch would evict a resident serving model, the operator confirms the eviction (which model is being displaced); a running training/HPO/batch job is never preempted — the switch is refused or deferred with a clear reason.

**Why this priority**: It hardens the switch against the platform's core constraint (Principle II) using the friction operators already know from preemptive swap. It's P3 because the earlier stories can be exercised from an idle state; this covers the contended case.

**Independent Test**: With a resident serving model, request a switch → the confirmation names the model to displace; the reload is sequential and never shows two models resident. With a training job holding the GPU, request a switch → it is refused/deferred and the job is untouched.

**Acceptance Scenarios**:

1. **Given** a serving model is resident, **When** the operator switches the served LLM, **Then** a confirmation names the model to be displaced before the eviction proceeds.
2. **Given** the operator confirms, **When** the switch runs, **Then** the reload is sequential (evict → load) and at no instant are two models resident.
3. **Given** a training/HPO/batch job holds the GPU, **When** a switch is requested, **Then** it is refused or deferred with a clear reason and the job is not preempted.

---

### Edge Cases

- **Base unresolved/unavailable** for a promoted adapter → promotion refused with a clear reason; the served LLM is unchanged (never a wedged empty state).
- **Promoting the already-serving version** → idempotent no-op reload; no gratuitous eviction or reload churn.
- **Load failure** (corrupt/missing artifact) → the platform surfaces the error and keeps (or restores) the previously-served LLM; serving is never left empty/wedged.
- **Rapid successive switches** → serialized; the platform converges to the last requested target and never goes two-resident mid-switch.
- **Pre-existing untagged LLM version** (registered before this feature) → still recognized and selectable as a text-generation target.
- **Switch requested during a running job** → refused/deferred, job untouched (never a preempt of a job).
- **Streamed predictions** → still carry the correct served model identity where they are recorded; the streaming path's existing capture limitations are unchanged and out of scope.

## Requirements *(mandatory)*

### Functional Requirements

**Registry-driven serving**

- **FR-254**: The served text-generation model MUST be determined by the registry's `@serving` text-generation target, resolved the same way the other engines resolve theirs — not by a fixed host-level configuration value.
- **FR-255**: Promoting a text-generation version to serving MUST change which model produces subsequent inference, with no host-level configuration edit and no manual process restart.
- **FR-256**: A text-generation model that is a full standalone model MUST remain servable unchanged; the non-adapter path preserves today's behavior byte-for-byte (companion to FR-263).

**Single-GPU lease (Principle II)**

- **FR-257**: Changing the served LLM MUST be a controlled, strictly sequential reload (evict → free → load) that never has two models resident in GPU VRAM at any instant.
- **FR-258**: If changing the served LLM would displace a currently-resident **serving** model, the operator MUST confirm the eviction (naming the model to be displaced) before it proceeds.
- **FR-259**: A running training/HPO/batch job MUST NOT be preempted by a served-LLM switch; the switch is refused or deferred with a clear, surfaced reason.

**Honest served identity**

- **FR-260**: The platform MUST report the model **name and version actually resident/serving** wherever serving identity is shown (GPU/lease status, serving view, inference response).
- **FR-261**: Each served prediction MUST be recorded against the model name + version that produced it, so the quality-monitoring window scores the correct model+version.
- **FR-262**: The inference response's model identity MUST match the registry identity of the served version — no divergence between an internal display alias and the registry record.

**Fine-tune (adapter) serving**

- **FR-263**: A registered LoRA fine-tune (adapter artifact plus a base reference) MUST be servable such that inference reflects the fine-tuned behavior — the adapter applied on its base.
- **FR-264**: The base for a promoted fine-tune MUST be resolved automatically from its recorded lineage; the operator MUST NOT have to hand-enter the base at serve time.
- **FR-265**: When a fine-tune's base cannot be resolved or is unavailable, the platform MUST refuse the promotion with a clear reason and leave the currently-served LLM unchanged.

**Discoverability & lineage**

- **FR-266**: Newly-registered text-generation fine-tunes MUST carry the descriptors needed to appear as selectable serving targets (task + serving engine + base/parent lineage); a valid LLM version MUST never present as an "unknown / no-renderer" placeholder.
- **FR-267**: Text-generation versions already registered without those descriptors MUST still be recognized as LLM versions and be selectable (backfilled or resolved by kind), so no valid-but-legacy version is stranded.
- **FR-268**: The console MUST present a text-generation version's base-vs-adapter nature and its lineage (base, parent, dataset) so the operator knows what they are promoting.

**Console operability**

- **FR-269**: The operator MUST be able to see, from the console, which text-generation model+version is **currently serving** versus which is **promoted** (`@serving`), including any resident-vs-promoted delta, and trigger the switch from the console.
- **FR-270**: The serving view MUST render a working inference panel for the promoted LLM (not a read-only placeholder) and reflect a completed switch.

**Contracts, safety, footprint**

- **FR-271**: The existing byte-compatible inference response contracts (single-shot and streaming) MUST be preserved across this change.
- **FR-272**: No gateway route may be reachable from the console that is not on the browser-facing allow-list; the allow-list delta (if any) MUST be explicit and auditable.
- **FR-273**: The change MUST NOT introduce a new always-on resident process and MUST stay within the platform's VRAM/RAM/disk budgets (Principle III).
- **FR-274**: A served LLM's identity and provenance (base + adapter, version, lineage) MUST be observable/recorded so a served configuration is reproducible (Principle VI).

### Key Entities

- **Text-generation serving target**: the registry `@serving` pointer for the LLM task, resolving to a specific model + version and the artifact set required to serve it (a full model artifact, **or** a base artifact + a LoRA adapter artifact).
- **LLM version**: a registered text-generation version; either a *full-model* kind or an *adapter* kind. Carries task and serving-engine descriptors, a base reference (for adapters), and lineage (parent, dataset, base).
- **Base reference**: how an adapter names the base it applies to, such that the base is resolvable at serve time from a known/registered artifact without operator entry.
- **Served identity**: the model name + version currently resident and producing inference — the single source of truth reported by serving status and stamped on each recorded prediction.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-143**: An operator can change which LLM serves entirely from the console (no host/shell access), and the next inference is produced by the newly-promoted model.
- **SC-144**: After a promotion, 100% of subsequent inferences are produced by the promoted version until it is changed again (no residual serving of the prior model).
- **SC-145**: For every served prediction, the recorded model+version equals the model+version that actually produced it — zero mislabeled predictions — so a quality check scores the correct model.
- **SC-146**: A promoted LoRA fine-tune's distinguishing trained behavior is observable in served inference; promoting back to the base removes it.
- **SC-147**: Switching the served LLM never results in two models resident at once — verifiable via lease/VRAM state observed through the switch (Principle II holds).
- **SC-148**: Every registered valid text-generation version is selectable as a serving target; none is shown as an unusable "no-renderer" placeholder.
- **SC-149**: The default base LLM continues to serve unchanged — existing single-shot and streaming inference behavior is byte-compatible for the non-adapter path.
- **SC-150**: A switch requested while a training/HPO/batch job holds the GPU is refused or deferred with a clear reason, and the job completes uninterrupted.
- **SC-151**: The console's "currently serving vs promoted" LLM view matches actual platform state within the live-status refresh cadence.

## Assumptions

- The registry already provides per-model `@serving` aliases and a gated promotion choke-point (features 011/015), and the other engines already resolve their served artifact from it. This feature routes the LLM through that **existing** mechanism rather than inventing a new one — the LLM becomes consistent with vision/embeddings/ASR/tabular.
- The GPU host agent's admission lease and controlled swap (features 017/018) are reused for the served-LLM reload; no new admission mechanism is introduced.
- The quality-monitoring window already keys on model name + version (feature 013); honest identity (US2) is what makes it correct for a served fine-tune.
- Base model artifacts for supported fine-tune bases are available locally (or resolvable) consistent with the frozen local model zoo and the disk budget (Principle III). The set of supported bases is bounded by what the platform already ships.
- The adapter-serving engine capability began as the `LORA` env prototype (commit `c28ca97` on this branch); this feature **generalizes** it from host-env-driven to registry-resolved (base + adapter from lineage) and is the first implemented increment.
- Single local operator; the existing key-injecting proxy is the only access control (no new auth/roles).
- The default served LLM remains a full base model unless an operator promotes a fine-tune; promotions are operator-initiated (this feature does not auto-promote LLMs — the existing retraining policy layer governs that).

## Dependencies

- **021 Loop-Native Console** — the Models promote gate, serving stage, and GPU-lease pill are the surfaces this feature makes the LLM honor.
- **011/015 Evaluation gates & registry** — the gated `@serving` promotion choke-point the LLM will now route through.
- **013 Quality monitoring** — the model+version-keyed window that honest identity keeps correct.
- **017/018 Swap & host-agent admission** — the controlled sequential reload / one-tenant lease reused for the switch.

## Out of Scope

- **Multi-LLM concurrent serving** — still one model in VRAM at a time (Principle II); this feature selects *which* one, not *how many*.
- **Non-LLM engines** — vision/embeddings/ASR/tabular already resolve from the registry; they are the reference behavior and are unchanged.
- **Streaming prediction capture limitations** (feature 016) — streamed predictions' logging/capture behavior is unchanged; only their *identity* correctness is in scope.
- **New training/fine-tuning capabilities** — this feature is about *serving* what is registered; training is unchanged except for the descriptor/lineage stamping needed for discoverability (FR-266).
- **Authentication / multi-user / roles** — unchanged.
