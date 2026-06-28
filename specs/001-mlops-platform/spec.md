# Feature Specification: MLOps-Lite Platform

**Feature Branch**: `001-mlops-platform`

**Created**: 2026-06-27

**Status**: Draft

**Input**: User description: "A self-hosted MLOps platform running on one machine. A user can: register and version datasets; launch and track training/fine-tuning runs on their own data; store, version, and compare models in a registry; submit inference requests (text/vision/audio) and retrieve results; promote a model to 'serving'; and monitor data/model drift, with drift or new data able to trigger a retrain. One model serves at a time, loaded on demand. Access is through a single API gateway plus the component web UIs."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run inference on demand (Priority: P1)

A user selects an available model, submits an input through the gateway, and receives a result.
The platform loads the model into the GPU on demand, runs inference, returns the output, and
frees VRAM afterward. The MVP serves a **text/LLM** model; additional modalities (vision, audio)
come online with the serving work in User Story 2.

**Why this priority**: This is the platform's most visible value and the minimum viable
product — a working "model store with an inference service." Everything else exists to keep
this stocked and reliable.

**Independent Test**: With one model pre-registered, POST an input to the gateway and confirm
a correct result is returned and that GPU VRAM is occupied only during the call.

**Acceptance Scenarios**:

1. **Given** a registered LLM, **When** the user submits a text prompt, **Then** a generated
   text response is returned and the request is recorded with status, latency, and model version.
2. **Given** no model is currently loaded, **When** a request arrives, **Then** the target model
   is loaded, used, and VRAM is released (or made reclaimable) after the response.
3. **Given** a model is already loaded for model A, **When** a request for model B arrives,
   **Then** the platform swaps to B without exceeding one resident model in VRAM.

---

### User Story 2 - Register, version, and promote models (Priority: P2)

A user publishes a model (a foundational model or a fine-tuned output) into a registry, where
it gets a name and an incrementing version. They can list versions, compare their metadata,
and promote one version to the "serving" stage so inference uses it.

**Why this priority**: The registry is the hinge of the lifecycle — training writes to it,
serving reads from it. Without it, serving has nothing curated to draw from.

**Independent Test**: Register two versions of a model, list them, promote v2 to serving, and
confirm subsequent inference (US1) uses v2.

**Acceptance Scenarios**:

1. **Given** a model artifact, **When** the user registers it, **Then** it appears in the
   registry with a unique name+version and recorded metadata (modality, size, metrics).
2. **Given** multiple versions, **When** the user promotes one to "serving", **Then** the
   serving stage resolves to exactly that version.

---

### User Story 3 - Register and version datasets (Priority: P3)

A user registers a dataset (files placed in object storage), and the platform records a
versioned reference so training runs can pin an exact dataset version.

**Why this priority**: Reproducible training depends on knowing exactly which data was used;
this enables Story 4 but isn't needed for serving alone.

**Independent Test**: Register a dataset, modify it, register again, and confirm two distinct
versions are retrievable and each resolves to the correct content.

**Acceptance Scenarios**:

1. **Given** a folder of data, **When** the user registers it, **Then** a named, versioned
   dataset entry is created pointing to immutable storage content.
2. **Given** a registered dataset, **When** the underlying data changes and is re-registered,
   **Then** a new version is created without destroying the prior version.

---

### User Story 4 - Fine-tune a model with experiment tracking (Priority: P4)

A user launches a fine-tuning run against a pinned dataset version and a base model. The run
is tracked (parameters, metrics, logs), and on success its output model is registered as a
new version (feeding Story 2).

**Why this priority**: Turns the platform from a serving system into a true MLOps platform,
but depends on the registry (US2) and datasets (US3) existing first.

**Independent Test**: Launch a small fine-tune on a tiny dataset, confirm the run records
parameters/metrics, and that a new model version appears in the registry on completion.

**Acceptance Scenarios**:

1. **Given** a base model and a pinned dataset version, **When** the user starts a fine-tune
   run, **Then** a tracked experiment run is created with live status and metrics.
2. **Given** a completed run, **When** it succeeds, **Then** its output model is registered as
   a new version linked back to the run and dataset version.
3. **Given** the resource budget, **When** a run executes, **Then** it uses at most the single
   GPU and does not exceed the VRAM budget.

---

### User Story 5 - Monitor drift and close the loop (Priority: P5)

A user views drift/quality and service-health dashboards for served models. When drift crosses
a threshold (or new labeled data arrives), the platform can trigger a retraining run (Story 4),
closing the lifecycle loop.

**Why this priority**: Highest-maturity capability; valuable but meaningful only once serving,
training, and the registry exist.

**Independent Test**: Feed reference vs. shifted data to the monitor, confirm a drift report is
produced and that crossing the threshold enqueues a retraining run.

**Acceptance Scenarios**:

1. **Given** a served model with a reference data profile, **When** new inference data drifts,
   **Then** a drift report is generated and surfaced on a dashboard.
2. **Given** a drift threshold breach, **When** the trigger fires, **Then** a retraining run is
   started referencing the current dataset version.

---

### Edge Cases

- **VRAM contention**: a second inference request arrives while a model is loaded — the platform
  must queue/serialize rather than load a second model concurrently (Principle II).
- **Disk pressure**: registering a model/dataset or pulling an image when free disk is low — the
  platform must fail clearly and not corrupt existing artifacts.
- **Model too large**: a requested model exceeds the GPU VRAM budget (`VRAM_GB`) — rejected at registration
  or serving time with a clear message, not an OOM crash.
- **Cold start latency**: first request to an unloaded model includes load time — surfaced
  distinctly from inference time.
- **Failed/cancelled training run**: leaves no partial model version in the registry and frees
  the GPU.
- **Offline operation**: after initial pulls, all core flows work with no internet.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The platform MUST expose a single API gateway as the entry point for inference and
  task submission, plus the component web UIs for registry, orchestration, and dashboards.
- **FR-002**: Users MUST be able to submit an inference request for a chosen model and modality
  (text, vision, audio) and retrieve the result and its metadata (status, latency, model version).
  Modality support is delivered incrementally: text/LLM first (US1), then vision and audio
  serving (US2).
- **FR-003**: The platform MUST load a model on demand and ensure at most one model occupies GPU
  VRAM at any time, releasing or making VRAM reclaimable after use.
- **FR-004**: The platform MUST reject (with a clear error) any model whose footprint exceeds the
  GPU VRAM budget, rather than failing with an out-of-memory crash.
- **FR-005**: Users MUST be able to register a model and receive an automatically versioned entry
  with recorded metadata; list and compare versions; and promote a version to "serving".
- **FR-006**: Inference MUST resolve the "serving" model version from the registry.
- **FR-007**: Users MUST be able to register datasets as named, versioned, immutable references
  in object storage.
- **FR-008**: Users MUST be able to launch a fine-tuning run against a pinned dataset version and
  base model; the run MUST be tracked with parameters, metrics, and logs.
- **FR-009**: A successful training run MUST register its output as a new model version linked to
  the originating run and dataset version.
- **FR-010**: The platform MUST generate data/model drift and quality reports for served models
  and surface them on a dashboard.
- **FR-011**: The platform MUST support triggering a retraining run when a drift threshold is
  breached or new data is registered.
- **FR-012**: Every experiment, model version, and dataset version MUST be persisted and
  retrievable so that a run is reproducible from its recorded configuration.
- **FR-013**: The entire platform MUST start from a single orchestration command and run on one
  machine with no required cloud service.
- **FR-014**: After initial image/model pulls, all core flows MUST function offline.
- **FR-015**: The platform MUST expose health and metrics for each service for observability.

### Key Entities *(include if feature involves data)*

- **Dataset / DatasetVersion**: a named training corpus and its immutable, versioned snapshots
  (pointer to stored content, item count, validation status).
- **Model / ModelVersion**: a registered model, its versions, modality, size, source run, and
  stage (e.g., none / serving).
- **Run** (canonical term; formerly "ExperimentRun"): a training/fine-tuning execution —
  parameters, metrics, logs, status, the dataset version used, and the produced model version.
- **InferenceRequest / Result**: a single prediction — input reference, chosen model version,
  status, output reference, latency, error (if any).
- **DriftReport**: a comparison of current vs. reference data/predictions for a served model,
  with computed drift/quality signals and threshold status.
- **ServingState**: which model version is currently designated for serving and what is resident
  in VRAM.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The full stack starts from a single command and reaches ready state on the
  target machine, with idle infrastructure consuming ≤ ~3 GB RAM.
- **SC-002**: At no time does the platform hold more than one model in GPU VRAM; a model swap
  between two models succeeds without exceeding the VRAM budget.
- **SC-003**: A user can go from "register a model" to "successful inference result" in under 10
  minutes on first use (excluding one-time image pulls).
- **SC-004**: A small fine-tuning run completes on the single GPU and automatically produces a
  new registered model version that can then be promoted and served.
- **SC-005**: Every served prediction and training run is retrievable later with the exact
  configuration used (model version, dataset version, parameters).
- **SC-006**: A drift threshold breach demonstrably triggers a retraining run end-to-end.
- **SC-007**: After initial setup, all P1–P2 flows succeed with networking disabled.

## Assumptions

- Single local operator (the developer); no multi-tenant auth, RBAC, or public exposure in v1.
- Model scope is limited to small or quantized models that fit `VRAM_GB` one at a
  time (e.g., quantized LLMs and nano/small vision and audio models sized to the profile).
- A CUDA-capable GPU is available to containers; GPU passthrough into the container runtime is a
  prerequisite verified before serving/training work.
- Free disk is limited (`FREE_DISK_GB`); the model zoo and image set are intentionally capped.
- Initial image and model-weight downloads require internet; steady-state operation does not.
- The single machine defined in the hardware profile (`.specify/memory/hardware-profile.md`) is
  the design target; the platform need not scale beyond one such machine.
