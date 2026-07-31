# Feature Specification: 026 Unified ML Lifecycle Console

**Feature Branch**: `claude/tech-stack-overview-i0cljp`

**Created**: 2026-07-31

**Status**: Draft

**Input**: User description: "Build a new modern UI for mlops-lite — a unified lifecycle console for
the gateway, host agent, MLflow, object store, evaluation layer, and observability stack, organized
around the model lifecycle rather than any one backend's resource hierarchy. Ten primary areas:
Overview, Models, Training, Evaluations, Deployments, Inference, Datasets, Runtime, Observability,
Administration."

## Summary

021 made the console's navigation *be* the lifecycle loop (`data → training → models → serving →
monitoring → retraining ⟲`). That framing succeeded at teaching the loop and failed at operating it:
the loop is a **narrative order**, not a **work surface**. Three of the platform's most operationally
consequential domains have no home in it — the GPU runtime (admission decisions, engine processes,
the durable journal), evaluation as a first-class activity, and the inference record (predictions,
traces, captures, labels). Operators route around the console into raw logs, the MLflow UI, and
Grafana precisely where the platform is most differentiated.

026 replaces the loop navigation with a **ten-area operational console** organized by *what the
operator is doing*, not by which backend owns the datum. The lifecycle loop survives as a
**visualization** — the Overview's normalized activity timeline and per-stage progress — rather than
as navigation.

The defining property is **one coherent lifecycle over five systems of record**. The gateway owns
predictions, labels, captures, jobs, policies, and suggestions. The host agent owns GPU topology,
admission, engines, and the journal. MLflow owns experiments, runs, logged and registered models, and
traces. The object store owns artifacts. Prometheus owns time series. The console joins them, and —
critically — **where two sources disagree it shows the disagreement rather than silently picking
one**.

Unlike 021 (front-end only, zero backend change), 026 is **full-stack**: the read surfaces the
console needs and the platform does not yet expose (per-device GPU topology, admission state as an
inspectable record, journal reads, trace reads, joined job state, endpoint records) are built as part
of this increment rather than rendered as "not available".

Scope is phase-gated per Principle VII. This spec defines all three maturity levels; **026 commits
to MVP 1 (the unified read console) built full-stack**, and specifies MVP 2 (lifecycle write actions)
and MVP 3 (operational intelligence) so the information architecture and contracts are designed
whole — those phase to 027/028.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ten-area console shell and Overview (Priority: P1) 🎯 MVP 1

An operator opens the console and lands on an Overview that answers four questions without a single
click: is the platform healthy, what is running, which models need attention, and what should I do
next. Navigation is ten workflow areas — Overview, Models, Training, Evaluations, Deployments,
Inference, Datasets, Runtime, Observability, Administration — each with its own secondary
navigation. A persistent header carries global search, an environment badge, an aggregate platform
health indicator, an active-job count, and a notification centre.

**Why this priority**: This is the increment's core intent and its MVP. Shipping only this replaces
the loop IA with a work-oriented one and gives the operator a real situational-awareness surface.
Every other story hangs off this shell.

**Independent Test**: Load the console against a running platform; confirm ten primary areas render
with correct secondary navigation, the Overview shows live summary cards, a unified active-work
table joining gateway jobs / host-agent jobs / MLflow runs, a severity-ranked attention panel, and a
normalized lifecycle activity timeline; confirm the health indicator aggregates all seven services
and opens a per-service panel.

**Acceptance Scenarios**:

1. **Given** the platform is running, **When** the operator loads the console, **Then** the Overview
   is the landing view and renders summary cards for active endpoints, running jobs, GPU
   utilization, pending admissions, failed jobs, models requiring review, unlabeled captures, and
   drift warnings — each showing its own data age.
2. **Given** a training job, an evaluation job, and a batch inference job are in flight, **When** the
   operator views the active-work panel, **Then** all three appear in one table with normalized
   status, progress, runtime, assigned device, and origin system, regardless of which backend
   reported them.
3. **Given** an engine has crashed and a model has failed its evaluation gate, **When** the operator
   views the attention panel, **Then** both are listed ranked by severity with a direct link to the
   diagnostic surface for each.
4. **Given** the host agent is unreachable, **When** the console loads, **Then** all ten areas remain
   navigable, the health indicator reports `degraded` naming the host agent, runtime-derived values
   render as `unknown` rather than zero, and the last successful values remain visible with their
   age.
5. **Given** the operator navigates to a retired 021 stage path, **When** the page resolves, **Then**
   they are redirected to the corresponding new area without a broken link.

---

### User Story 2 - Runtime and GPU console (Priority: P1) 🎯 MVP 1

An operator inspects the GPU runtime directly: which hosts are alive, what each device holds, which
engine processes are resident, what is waiting for admission and **why**, and what the durable
journal recorded. Admission decisions are rendered as human-readable explanations, not status codes.

**Why this priority**: The host agent's in-process admission and single-tenant GPU discipline is the
platform's defining constraint (Principle II) and today has no operator surface beyond a status
pill. This is the single largest capability gain over a reskinned MLflow interface, and the largest
net-new backend surface in 026.

**Independent Test**: With the agent running, open Runtime; confirm host and per-device state
(index, name, UUID, compute capability, total/free/used VRAM, utilization, resident processes), the
engine process table, an admission view showing decisions with reasons, and a queryable journal.
Submit a job that cannot be admitted and confirm the refusal is explained in prose naming the
blocking tenant and the VRAM shortfall.

**Acceptance Scenarios**:

1. **Given** the agent is running with an LLM engine resident, **When** the operator opens the host
   detail, **Then** each GPU device shows its live VRAM breakdown and the resident engine is listed
   with its process id, modality, model identity, and device.
2. **Given** a training job holds the GPU and a vision inference request arrives, **When** the
   operator views the admission surface, **Then** the refusal is stated in prose — naming the
   holding job and that a running job is never preempted — not as a bare status code.
3. **Given** a job requests more VRAM than any device has free, **When** the operator inspects the
   decision, **Then** the explanation names the required amount, the largest free block, and the
   device evaluated.
4. **Given** the agent restarted mid-job, **When** the operator opens the journal, **Then** the
   interrupted job's transitions are visible in sequence with the replay outcome, and the entry is
   not silently absent.
5. **Given** live GPU reads are unavailable and the agent is on its static-budget fallback, **When**
   the operator views GPU state, **Then** the console labels the values as fallback-derived rather
   than presenting them as measured.

---

### User Story 3 - Model catalog and runtime compatibility (Priority: P1) 🎯 MVP 1

An operator browses one model catalog that unifies MLflow logged models, registered models and
versions, object-store artifact metadata, gateway deployment assignments, and evaluation results —
then opens a model to see, explicitly, whether this platform can actually run it.

**Why this priority**: "Can this artifact run here, and what happens if I promote it?" is the
question the current console cannot answer. Compatibility must be derived from real contracts and
host topology, never inferred from modality alone.

**Independent Test**: Open the catalog; confirm rows join registry, artifact, deployment, and
evaluation facts across all five modalities. Open a text-generation version and confirm the
compatibility panel reports required engine, hardware requirements, artifact availability, estimated
VRAM against the largest free block, and a resulting eligibility verdict.

**Acceptance Scenarios**:

1. **Given** models exist across all five served modalities, **When** the operator opens the catalog,
   **Then** each row shows model, modality, registry version, aliases, source run, evaluation status,
   deployment count, runtime engine, hardware requirement, artifact size, and last activity.
2. **Given** an adapter version whose base model cannot be resolved from lineage, **When** the
   operator opens it, **Then** the compatibility panel reports the artifact as not servable and names
   the unresolvable base, matching the platform's refusal behaviour.
3. **Given** a version whose estimated VRAM exceeds the largest free block, **When** the operator
   views compatibility, **Then** the verdict is `not currently eligible` with the shortfall stated,
   and it is distinguished from a structural incompatibility.
4. **Given** a registered version whose artifact is missing from the object store, **When** the
   operator opens it, **Then** the console reports the missing artifact rather than rendering a
   broken download.
5. **Given** the operator opens a model's lineage, **When** the version derives from a fine-tune,
   **Then** the chain back to its base model and source run is navigable.

---

### User Story 4 - Training workspace (Priority: P2) 🎯 MVP 1

An operator follows a training job end to end: the gateway job record, the host-agent execution
record, the MLflow run, the hyperparameter study when present, and the resulting registered version —
joined into one view with a normalized state, an execution timeline, a resource panel, and streaming
logs.

**Why this priority**: Training is where the platform's multi-system nature is most confusing today —
the same unit of work has three identifiers and three different status vocabularies.

**Independent Test**: Launch a fine-tune; confirm the job appears with a normalized state, that its
timeline advances through submitted → validated → admission evaluated → engine allocated → process
started → run created → completed → artifacts persisted → finalized, that the resource panel shows
the assigned device and VRAM, and that logs stream incrementally with follow, pause, search, severity
filter, and download.

**Acceptance Scenarios**:

1. **Given** a running fine-tune, **When** the operator opens the job, **Then** the gateway state,
   agent state, and MLflow run state are all shown alongside a single normalized state.
2. **Given** a hyperparameter study, **When** the operator opens it, **Then** trials, optimization
   history, parameter importance, the best trial, and each trial's child run are available, and the
   console does not imply a persistent search service exists.
3. **Given** a job was rejected at admission, **When** the operator opens it, **Then** the normalized
   state is `Rejected` and the admission reason is shown inline.
4. **Given** the log stream is interrupted, **When** the operator is in follow mode, **Then** the
   console reports the interruption and resumes without silently dropping lines.

---

### User Story 5 - Evaluations, quality gates, and drift (Priority: P2) 🎯 MVP 1

An operator reviews evaluation runs across modalities, inspects gate outcomes with the rule that
produced them, compares versions and runs, and reads drift honestly — including what the drift
statistic does not prove.

**Why this priority**: Gated promotion is the platform's central safety mechanism and is currently
visible only as a pass/fail at the point of promotion. Drift is presented without its caveats.

**Independent Test**: Open Evaluations; confirm the list joins evaluation results with model,
version, modality, dataset, metrics, gate result, and source job; open a failed gate and confirm the
failing rule, threshold, observed value, and comparison basis are shown; open Drift and confirm drift
values render with configurable thresholds and stated limitations.

**Acceptance Scenarios**:

1. **Given** evaluations exist for several modalities, **When** the operator views the list, **Then**
   each shows its modality-appropriate metrics rather than a forced common metric.
2. **Given** a version failed its quality gate, **When** the operator opens the result, **Then** the
   specific rule, operator, threshold, observed value, incumbent compared against, and metric
   direction are shown.
3. **Given** a gate was bypassed by operator override, **When** the operator views the evaluation,
   **Then** the override and its recorded reason are visible.
4. **Given** a drift report exists, **When** the operator views it, **Then** thresholds are shown as
   configurable, per-feature contributions are listed, and the limitations of the statistic are
   stated on the surface itself.
5. **Given** a version has no logged metric and is not the serving version, **When** the operator
   views its evaluation state, **Then** it reads as `not evaluated` and the platform's refusal to
   score it is explained rather than shown as an error.

---

### User Story 6 - Inference workspace (Priority: P2) 🎯 MVP 1

An operator inspects the inference record: predictions from the gateway store, traces as a diagnostic
waterfall, captured samples, label state, and the review queue — with payload handling that treats
served inputs and outputs as sensitive by default.

**Why this priority**: The prediction/label/capture triple is the feedback loop's raw material and
has no read surface today beyond aggregate quality numbers.

**Independent Test**: Serve traffic; confirm predictions list with model version, latency, capture
and label state, and trace linkage; open one and confirm payloads are hidden by default with an
explicit reveal; open its trace and confirm a span waterfall linked back to the prediction.

**Acceptance Scenarios**:

1. **Given** predictions exist, **When** the operator views the table, **Then** rows come from the
   gateway record — not reconstructed from traces — and each shows model version, modality, latency,
   capture state, and label state.
2. **Given** a prediction has a captured payload, **When** the operator opens it, **Then** input and
   output are hidden by default, revealed only by explicit action, never present in the address, and
   never emitted to browser telemetry.
3. **Given** a trace exists for a prediction, **When** the operator opens it, **Then** a span
   waterfall renders with durations, attributes, events, errors, and links back to the prediction and
   model version.
4. **Given** a non-text-generation modality, **When** the operator opens its trace, **Then** the
   presentation is generic and does not assume token-oriented spans.
5. **Given** unlabeled captures exist, **When** the operator opens the review queue, **Then** items
   are prioritized by policy result, drift contribution, sampling strategy, and manual flags.

---

### User Story 7 - Deployments read-side (Priority: P2) 🎯 MVP 1

An operator sees every logical endpoint, which model version each serves, the runtime actually
backing it, and its live traffic health — with the gateway's logical assignment and the agent's
runtime process distinguished rather than conflated.

**Why this priority**: "What is actually serving right now" currently requires reading the serving
pointer, the agent health, and the registry separately.

**Independent Test**: Open Deployments; confirm endpoints list with modality, assigned model, alias
or version, runtime, host, status, request rate, error rate, and P95 latency; open one and confirm
the model-assignment view distinguishes desired from resident state.

**Acceptance Scenarios**:

1. **Given** an endpoint is assigned a version whose activation is still in progress, **When** the
   operator views it, **Then** desired and resident are shown separately and the endpoint is not
   labeled healthy on the strength of the desired state alone.
2. **Given** an endpoint's model is not resident because the GPU is held elsewhere, **When** the
   operator views its status, **Then** the status reflects on-demand loading rather than reporting a
   failure.
3. **Given** rollout controls are not implemented by the gateway, **When** the operator views an
   endpoint, **Then** no decorative traffic-splitting control is displayed.

---

### User Story 8 - Datasets and artifacts (Priority: P3) 🎯 MVP 1

An operator browses content-addressed dataset versions and model artifacts with size, digest,
validation status, and the runs and models that reference them — and can preview or download through
trusted server-side code without the browser ever holding object-store credentials.

**Why this priority**: Valuable and heavily used, but the existing data stage already covers the
basic path; this is a deepening rather than a gap.

**Independent Test**: Open Datasets; confirm logical name, digest, size, object count, format,
schema and validation status, and referencing runs/models; download a dataset and confirm the bytes
are proxied server-side and no credentials reach the browser.

**Acceptance Scenarios**:

1. **Given** a dataset version, **When** the operator opens it, **Then** its content digest,
   validation result, and every referencing run and model version are listed.
2. **Given** an artifact with a recorded checksum, **When** the operator views it, **Then** integrity
   reads as verified, failed, not verified, or verification unavailable — never silently absent.
3. **Given** an operator requests an artifact path outside the permitted prefixes, **When** the
   request is made, **Then** it is rejected server-side and not forwarded.

---

### User Story 9 - Observability and Administration (Priority: P3) 🎯 MVP 1

An operator reads curated platform metrics natively in the console, opens the external dashboard tool
for deep exploration, and inspects alert-rule state honestly. Administration exposes storage,
database, integrations, API access, and system information.

**Why this priority**: The external dashboard tool already covers deep exploration; the native panels
are for keeping the operator in one place for routine checks.

**Independent Test**: Open Observability; confirm native panels for request rate, error rate, latency
percentiles, active jobs, queue depth, GPU utilization and VRAM, engine restarts, and per-service
health; confirm alert rules render with state; confirm the embedded dashboard loads within the
configured frame policy and degrades to an external link when embedding is unavailable.

**Acceptance Scenarios**:

1. **Given** an alert rule is firing, **When** the operator views alerts, **Then** the state is shown
   and the console does **not** claim a notification was delivered, because no notification channel
   exists.
2. **Given** dashboard embedding is blocked, **When** the operator opens a dashboard panel, **Then** a
   clear fallback with an external-open action is shown instead of an empty frame.
3. **Given** the migration ledger has an applied history, **When** the operator opens Administration,
   **Then** applied migrations, their checksum state, and the current schema version are visible.

---

### User Story 10 - Truthful state: conflicts, degradation, and mode (Priority: P2) 🎯 MVP 1

The console never presents a confident answer it cannot support. When joined records disagree it
surfaces the conflict; when a service is down it degrades per-domain instead of failing whole; and a
persistent badge states whether the operator is looking at fixture, live, or hardware-backed data.

**Why this priority**: This is the property that makes a multi-backend console trustworthy. Without
it, every other story is capable of confidently displaying a falsehood.

**Independent Test**: Force a disagreement (gateway reports a job running while the agent has no
process) and confirm a conflict banner naming both sources and the last consistent timestamp; stop
each backing service in turn and confirm the documented per-service degradation; run in offline
fixture mode and confirm the mode badge is unmistakable.

**Acceptance Scenarios**:

1. **Given** the gateway reports a job `running` and the host agent reports no process, **When** the
   operator views the job, **Then** a conflict banner shows both source states, the run state, and
   the last consistent timestamp, with actions to refresh and inspect the journal.
2. **Given** MLflow is unreachable, **When** the operator uses the console, **Then** deployment and
   runtime views continue to work and only experiment and registry surfaces degrade.
3. **Given** the host agent is unreachable, **When** the operator views runtime state, **Then** it
   reads `unknown` and the console does **not** assert that jobs have stopped.
4. **Given** the console is running against fixtures, **When** any page renders, **Then** a
   persistent mode badge marks the data as non-live.
5. **Given** a poll fails repeatedly, **When** the operator watches a live surface, **Then** backoff
   is applied, the last successful value is retained with its age, and staleness is never rendered as
   zero activity.

---

### User Story 11 - Lifecycle write actions (Priority: P3) — MVP 2, phases to 027

The operator acts from the console: start and cancel supported jobs, register versions, assign
aliases, create endpoint assignments, submit labels, approve review items, and manage capture and
evaluation policies.

**Why this priority**: Deliberately deferred. MVP 1 must prove the read model and the conflict
semantics before write paths are layered on; mutating state across five systems of record without a
trustworthy read surface is how a console corrupts a platform.

**Independent Test**: Each action is independently testable against its owning backend, with an
optimistic-update rollback on failure.

**Acceptance Scenarios**:

1. **Given** the read console is trustworthy, **When** an operator assigns an alias, **Then** the
   change is written through the gateway's gated promotion path — never by writing the registry
   directly around the gate.
2. **Given** an action fails upstream, **When** the operator has seen an optimistic update, **Then**
   the update is rolled back and the upstream error is surfaced verbatim.

---

### User Story 12 - Operational intelligence (Priority: P3) — MVP 3, phases to 028

Drift workflows, suggestion review with evidence, quality-gate authoring, automated cross-system
reconciliation, controlled rollout and rollback, and audit views.

**Why this priority**: Highest-leverage once the platform is legible, and meaningless before it.

**Independent Test**: Each capability is independently demonstrable against the gateway's existing
policy and suggestion records.

**Acceptance Scenarios**:

1. **Given** a suggestion exists, **When** the operator reviews it, **Then** its supporting evidence
   is navigable and accepting it is recorded as an operator decision, never auto-applied.

---

### Edge Cases

- **Every backend down at once.** The shell must still render, name every unreachable service, and
  offer no view that implies data was fetched.
- **Partial multi-source join.** A model present in the registry but absent from the object store, or
  a gateway job with no tracking run, must render with the missing side marked absent — not dropped
  from the list.
- **Clock skew between gateway and agent.** Timelines built from two clocks must not render negative
  durations or reorder events; skew beyond a threshold is disclosed.
- **A single host with multiple devices vs. multiple hosts.** The contract must accommodate both even
  though the reference deployment is one host with one device.
- **Very large captured payloads.** Previews must truncate with the true size stated, never attempt to
  load the whole object into the browser.
- **A model version whose modality tag is missing or unrecognized.** It must appear in the catalog as
  `unknown modality`, not be silently filtered out.
- **Journal growth.** The journal view must page and filter rather than loading the full log.
- **Alias moved while the operator is viewing a version.** The stale view must be detectable and the
  operator told what changed rather than acting on a stale alias.
- **Browser tab hidden for hours.** Polling must suspend and, on return, refresh before showing
  anything as current.
- **A retired 021 path with no one-to-one successor.** It must resolve to the nearest area rather than
  returning not-found.

## Requirements *(mandatory)*

### Functional Requirements

#### Information architecture and shell (US1)

- **FR-362**: The console MUST present exactly ten primary areas — Overview, Models, Training,
  Evaluations, Deployments, Inference, Datasets, Runtime, Observability, Administration — each with
  the secondary navigation defined in this spec.
- **FR-363**: The 021 loop navigation MUST be removed as a navigation mechanism; the lifecycle loop
  MUST remain visible as a normalized activity timeline and per-stage progress on the Overview.
- **FR-364**: Every retired 021 stage path MUST redirect to its successor area; no retired path may
  return not-found.
- **FR-365**: Navigation MUST NOT be organized by backend system; no primary or secondary navigation
  item may be named after a backing service.
- **FR-366**: The tracking system's operational vocabulary — experiment, run, logged model, registered
  model, model version, alias, trace — MUST be preserved verbatim where displayed, not renamed into
  proprietary equivalents.
- **FR-367**: The global header MUST provide sidebar toggle, product name, environment badge, global
  search, aggregate platform health, active-job count, notification centre, and user menu.
- **FR-368**: Global search MUST resolve models, runs, datasets, jobs, endpoints, and predictions by
  identifier or name from a single input.
- **FR-369**: The aggregate health indicator MUST derive from gateway, tracking, database, object
  store, host-agent, metrics, and GPU availability, resolving to exactly one of `healthy`,
  `degraded`, `critical`, or `unknown`, and MUST open a per-service detail panel.
- **FR-370**: `critical` MUST be reserved for states in which training or inference cannot operate
  safely; optional-service impairment MUST resolve to `degraded`.
- **FR-371**: The Overview MUST render summary cards for active endpoints, running jobs, GPU
  utilization, pending admissions, failed jobs, models requiring review, unlabeled captures, and
  drift warnings.
- **FR-372**: The Overview MUST render a unified active-work table joining gateway jobs, host-agent
  jobs, and tracking runs into one normalized row shape.
- **FR-373**: The Overview MUST render an attention panel ranked by severity covering at least engine
  crash, GPU memory pressure, failed training run, evaluation-gate failure, significant drift,
  unlabeled-capture backlog, registry version without signature, deployment referencing a missing
  artifact, and stale host-agent heartbeat.

#### Runtime and GPU (US2)

- **FR-374**: The console MUST expose per-host runtime state: agent status, device count, available
  devices, active engines, active jobs, journal state, and last heartbeat.
- **FR-375**: The console MUST expose per-device state: index, name, unique identifier, compute
  capability, total, free and used VRAM, utilization, temperature where available, and resident
  processes.
- **FR-376**: The console MUST expose resident engine processes with engine identifier, modality,
  model identity, process identifier, device, VRAM, start time, health, and active request count.
- **FR-377**: The console MUST expose admission requests and decisions with the required engine,
  requested VRAM, device evaluated, decision, reason, and age.
- **FR-378**: Every admission decision MUST be rendered as a human-readable explanation naming the
  determining factor — the blocking tenant, the VRAM shortfall, or the checks that passed.
- **FR-379**: The console MUST NOT offer any control that would preempt a running job; refusal
  semantics MUST be presented as the platform's designed behaviour.
- **FR-380**: The console MUST expose the durable journal as a paged, filterable diagnostic view with
  sequence, timestamp, event type, job, engine, process, device, state transition, details, and
  checksum state — never as a raw file dump.
- **FR-381**: When GPU values derive from the static-budget fallback rather than a live read, the
  console MUST label them as fallback-derived.
- **FR-382**: Runtime contracts MUST accommodate multiple hosts and multiple devices per host even
  where the reference deployment has one of each.

#### Models (US3)

- **FR-383**: The model catalog MUST join logged models, registered models and versions, object-store
  artifact metadata, gateway deployment assignments, and evaluation results into one list.
- **FR-384**: Catalog rows MUST show model, modality, registry version, aliases, source run,
  evaluation status, deployment count, runtime engine, hardware requirement, artifact size, and last
  activity.
- **FR-385**: All five served modalities — text generation, image classification, embeddings,
  automatic speech recognition, tabular — MUST be representable, and an unrecognized modality MUST
  render as `unknown` rather than being filtered out.
- **FR-386**: The model detail view MUST provide Overview, Versions, Evaluations, Deployments,
  Training, Inference, Artifacts, Lineage, and Activity.
- **FR-387**: The compatibility panel MUST derive its verdict from gateway contracts and live host
  topology — required engine, accelerator requirement, required compute capability, artifact
  availability, host compatibility, estimated VRAM, largest free VRAM block, admission result — and
  MUST NOT infer compatibility from modality alone.
- **FR-388**: The compatibility verdict MUST distinguish `not currently eligible` (a transient
  resource condition) from `incompatible` (a structural mismatch).
- **FR-389**: For adapter versions, the console MUST resolve and display the base model from lineage,
  and MUST report an unresolvable base as not servable, matching platform refusal behaviour.
- **FR-390**: Model lineage MUST be navigable from a fine-tuned version back through its chain to a
  base model and source run.

#### Training (US4)

- **FR-391**: A training job view MUST join the gateway job record, the host-agent execution record,
  the tracking run, and the hyperparameter study where present.
- **FR-392**: Job state MUST be normalized to a single vocabulary — Draft, Queued, Admission Check,
  Admitted, Starting, Running, Completing, Succeeded, Failed, Cancelled, Rejected, Orphaned, Unknown
  — while each source's native state remains inspectable.
- **FR-393**: The job view MUST render an execution timeline from submission through validation,
  admission evaluation, engine allocation, process start, run creation, completion, artifact
  persistence, evaluation initiation, and finalization.
- **FR-394**: The job view MUST render a resource panel with device name, index, total, free, reserved
  and process VRAM, processor and memory where available, and child process identifier.
- **FR-395**: Logs MUST stream incrementally and support follow, pause, search, severity filtering,
  download, and copy; an interrupted stream MUST be reported, not silently truncated.
- **FR-396**: Hyperparameter studies MUST present parallel coordinates, optimization history,
  parameter importance, a trial table, the best trial, and per-trial child runs.
- **FR-397**: The console MUST NOT imply the existence of a persistent orchestration or
  hyperparameter-search control plane; ephemeral execution MUST be presented as such.

#### Evaluations and drift (US5)

- **FR-398**: The evaluation list MUST show evaluation, model, version, modality, dataset, metrics,
  gate result, creation time, and source job.
- **FR-399**: Metrics MUST be presented per modality using that modality's own primary metric, not
  coerced into a single cross-modality metric.
- **FR-400**: A gate outcome MUST resolve to exactly one of passed, failed, warning, not evaluated,
  or incomplete, and a failure MUST show the failing rule, operator, threshold, observed value,
  comparison basis, and metric direction.
- **FR-401**: An operator override of a gate MUST be displayed together with its recorded reason.
- **FR-402**: A version with no logged metric that is not the serving version MUST read as `not
  evaluated`, with the platform's refusal explained rather than rendered as an error.
- **FR-403**: The comparison workspace MUST separate quality, latency, resource usage, artifact
  differences, dataset differences, and policy compliance.
- **FR-404**: Drift MUST show model, endpoint, reference and current windows, feature count, maximum
  drift statistic, features in warning, features in critical state, and last calculation time.
- **FR-405**: Drift thresholds MUST be configurable rather than hard-coded, and their values in force
  MUST be visible on the surface.
- **FR-406**: The drift surface MUST state the limitations of the drift statistic — that it detects
  distributional change, does not prove quality degradation, does not establish causality, and
  depends on the chosen baseline and binning.

#### Inference (US6)

- **FR-407**: The predictions table MUST be sourced from the gateway prediction record, not
  reconstructed from traces, and MUST show prediction identifier, timestamp, endpoint, model version,
  modality, status, latency, capture state, label state, trace linkage, and policy result.
- **FR-408**: Prediction detail MUST show request metadata, input and output previews subject to
  policy, model identity, runtime information, latency breakdown, capture state, label, trace
  relationship, policy decisions, and error details.
- **FR-409**: Payloads MUST be hidden by default, support field redaction and truncated previews,
  require an explicit reveal action, and MUST NOT appear in addresses or browser telemetry.
- **FR-410**: Label state MUST resolve to unlabeled, pending review, labeled, disputed, or excluded.
- **FR-411**: The review queue MUST prioritize by policy result, low confidence, drift contribution,
  sampling strategy, missing label, manual flag, and gateway suggestion.
- **FR-412**: Traces MUST render as a span waterfall with hierarchy, durations, inputs and outputs,
  attributes, events, errors, and links to the originating prediction and model version.
- **FR-413**: Trace presentation MUST remain generic for non-text-generation modalities and MUST NOT
  assume token-oriented spans.

#### Deployments (US7)

- **FR-414**: The endpoint list MUST show endpoint, modality, assigned model, alias or version,
  runtime, host, status, traffic, request count, error rate, tail latency, and last update.
- **FR-415**: Endpoint status MUST resolve to unconfigured, pending, starting, healthy, degraded,
  draining, stopped, failed, or unknown.
- **FR-416**: Desired assignment and resident runtime state MUST be displayed separately; an endpoint
  MUST NOT be reported healthy on the basis of desired state alone.
- **FR-417**: An endpoint whose model is not resident because the GPU is held elsewhere MUST be
  presented as on-demand, not as failed.
- **FR-418**: Only rollout controls the gateway actually implements may be displayed; no decorative
  traffic-splitting control may be rendered.

#### Datasets and artifacts (US8)

- **FR-419**: Dataset versions MUST display logical name, content digest, size, object count, format,
  schema status, validation status, creation time, and referencing runs and models.
- **FR-420**: Artifact integrity MUST resolve to verified, verification failed, not verified, or
  verification unavailable.
- **FR-421**: Object-store credentials MUST NOT reach the browser under any circumstance; artifact
  bytes MUST be proxied by trusted server-side code.
- **FR-422**: Artifact and dataset paths MUST be validated server-side against an allowlist of
  permitted prefixes before any upstream request is made.

#### Observability and administration (US9)

- **FR-423**: The console MUST render native panels for request rate, error rate, latency
  percentiles, active jobs, queue depth, GPU utilization, GPU VRAM, engine restarts, and tracking,
  object-store, and database request health.
- **FR-424**: Alert-rule state MUST resolve to inactive, pending, firing, or unknown, and the console
  MUST NOT claim any notification was delivered.
- **FR-425**: Embedded dashboards MUST load within the configured frame policy, expose no
  administrative controls, be explicitly labeled as external dashboards, offer an external-open
  action, and degrade to a link when embedding is unavailable.
- **FR-426**: Administration MUST surface storage, database, integrations, API access, and system
  information, including applied schema migrations with their checksum state and the current schema
  version.

#### Truthfulness, degradation, and data access (US10)

- **FR-427**: Where two systems of record disagree about the same entity, the console MUST display
  the disagreement — both source states and the last consistent timestamp — and MUST NOT silently
  select one.
- **FR-428**: Per-service degradation MUST follow the documented matrix: tracking loss preserves
  deployment and runtime views; gateway loss enters read-limited mode; host-agent loss marks runtime
  unknown without asserting jobs stopped; object-store loss disables artifact previews; metrics loss
  removes historical charts but retains current direct state; dashboard loss hides the embed;
  database loss is a critical platform failure.
- **FR-429**: A persistent mode badge MUST state whether data is fixture-backed, live, or
  hardware-backed.
- **FR-430**: Every live surface MUST display its data age and MUST retain the last successful value
  during a transient outage; stale data MUST NOT be rendered as zero.
- **FR-431**: Polling MUST suspend while the browser tab is hidden and MUST apply exponential backoff
  on failure.
- **FR-432**: The browser-facing proxy MUST inject gateway and agent credentials server-side, restrict
  upstream targets to an allowlist, enforce request timeouts, attach correlation identifiers,
  normalize upstream errors, and filter sensitive fields.
- **FR-433**: Backend capability MUST be resolved server-side and expressed to the interface as
  feature availability, so an unsupported capability is absent rather than failing on use.
- **FR-434**: The console MUST NOT introduce a message broker, workflow scheduler, analytics
  datastore, or additional model-serving runtime.

#### Phased scope (US11, US12)

- **FR-435**: MVP 2 write actions MUST route through the owning backend's sanctioned path — in
  particular, alias assignment MUST go through the gated promotion endpoint and never write the
  registry directly around the gate.
- **FR-436**: MVP 2 optimistic updates MUST roll back on upstream failure and surface the upstream
  error verbatim.
- **FR-437**: MVP 3 suggestions MUST be presented as recommendations carrying their supporting
  evidence, with acceptance recorded as an operator decision and never auto-applied.
- **FR-438**: Any new externally observable endpoint added by this feature MUST land with a
  corresponding contract update, and any schema change MUST land as a new numbered migration.

### Key Entities

- **PlatformModel**: the unified model identity across systems — logged model, registered model,
  version, aliases, source run, artifact reference and digest, modality, evaluation state,
  deployments, and runtime requirements.
- **PlatformJob**: one unit of work with its gateway state, agent state, tracking run, normalized
  state, assigned host and device, timestamps, and any detected state conflict.
- **PlatformHealth**: overall platform state plus per-service health and the observation time.
- **RuntimeHost / RuntimeDevice / EngineProcess**: the host agent's topology — hosts, GPU devices with
  live VRAM and utilization, and resident engine child processes.
- **AdmissionRecord**: a request for the GPU with its requested resources, evaluated device, decision,
  human-readable reason, and age.
- **JournalEntry**: one durable state transition with sequence, timestamp, event type, subject, state
  change, and checksum state.
- **EvaluationResult**: a scored version with its modality-appropriate metrics, benchmark identity and
  digest, gate outcome, and any override.
- **QualityGate / GateRule**: a named, scoped, modality-specific set of metric rules with operator,
  thresholds, required flag, and enabled flag.
- **DriftReport**: reference and current windows, per-feature statistics, maximum statistic,
  thresholds in force, and calculation time.
- **Endpoint**: a logical serving assignment with modality, assigned version, desired versus resident
  state, runtime, host, status, and traffic health.
- **PredictionRecord**: a served prediction with model identity, latency, capture state, label state,
  policy result, and trace linkage.
- **CaptureSample / Label**: a recoverable served input under capture policy and its ground-truth
  label with review state.
- **DatasetVersion / Artifact**: content-addressed data and model objects with digest, size,
  validation status, integrity state, and references.
- **Policy / Suggestion**: a declarative rule and a generated recommendation with its evidence and
  review state.
- **StateConflict**: a detected disagreement between systems of record — the entity, the disagreeing
  sources and their states, and the last consistent time.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-184**: An operator can determine platform health, what is running, and what needs attention
  from the landing view alone, without navigating, in under 15 seconds.
- **SC-185**: Every one of the ten primary areas is reachable in one interaction from any other area.
- **SC-186**: 100% of retired 021 navigation paths resolve to a successor area; none return
  not-found.
- **SC-187**: For every admitted and refused GPU request during a validation session, the console
  presents a human-readable reason naming the determining factor — no bare status codes.
- **SC-188**: An operator can answer "can this model version run on this host right now, and if not
  why" from the model detail view alone, for all five served modalities.
- **SC-189**: A training job's gateway record, agent execution, and tracking run are reachable from
  one another in at most one interaction each.
- **SC-190**: 100% of displayed job states map to the normalized vocabulary, and each source's native
  state remains inspectable.
- **SC-191**: For every failed quality gate, the failing rule, threshold, and observed value are
  visible without leaving the evaluation view.
- **SC-192**: No served input or output payload is displayed without an explicit reveal action, and
  no payload value appears in any address — verified by inspection across every payload surface.
- **SC-193**: With each backing service stopped in turn, the console remains navigable and the
  documented degradation for that service holds — verified for all seven services.
- **SC-194**: An induced disagreement between gateway and host-agent job state produces a visible
  conflict disclosure in 100% of cases, never a silently chosen single answer.
- **SC-195**: Every live surface displays its data age, and no surface renders stale data as zero
  activity.
- **SC-196**: Polling ceases entirely while the browser tab is hidden.
- **SC-197**: No object-store or gateway credential is retrievable from the browser — verified by
  inspecting all client-delivered payloads.
- **SC-198**: The production interface bundle adds no new runtime dependency beyond the existing
  framework, view library, and styling toolchain.
- **SC-199**: Idle platform memory remains within the constitution's footprint budget with the new
  console running.
- **SC-200**: The full offline test suite, linting, interface build, compose render, and spec checks
  all pass, with no regression against the pre-026 baseline.
- **SC-201**: **[HW]** On the target GPU machine, live per-device VRAM, resident engine identity, and
  admission decisions displayed by the console match the agent's own reported state exactly.
- **SC-202**: **[HW]** During a real single-tenant contention event, the console's runtime view
  reflects the correct holder and the correct refusal reason throughout.
- **SC-203**: Every new externally observable endpoint introduced by this feature has a corresponding
  contract entry.

## Assumptions

- **Increment numbering**: this is increment 026, continuing the global sequence at FR-362, SC-184,
  and T618. 025's outstanding hardware task is unrelated and remains open on its own.
- **Scope commitment**: 026 delivers MVP 1 — the unified read console — built full-stack, meaning the
  read endpoints it requires are implemented rather than stubbed. MVP 2 (US11) and MVP 3 (US12) are
  specified here for architectural coherence and phase to 027/028 per Principle VII.
- **The loop is retained as meaning, not navigation**: no lifecycle stage becomes unreachable, so
  Principle IV is preserved; the change is organizational.
- **Single operator, no user accounts**: the platform authenticates with a shared API key and has no
  user identity model. Ownership, "approved by", and permission checks therefore degrade to a single
  operator identity; a real multi-user model is out of scope and would require its own increment.
- **Environment badge values** map to the repository's existing test and deployment taxonomy —
  offline (fixtures), live (compose stack), and hardware (GPU host attached) — rather than a
  cloud-style development/staging/production ladder, which does not apply to a local-first
  single-machine platform.
- **Multi-host is contract-only**: the reference deployment has one host with one GPU; the runtime
  contract accommodates more but no multi-host orchestration is built.
- **Visual direction**: a modern console aesthetic replaces the terminal/man-page styling, with data
  visualization implemented without adding a charting dependency, honouring the lightweight-footprint
  principle.
- **Existing backends are authoritative**: this feature adds read surfaces over existing platform
  state and does not change admission semantics, promotion gating, capture policy, or any GPU
  behaviour.
- **The external dashboard tool remains** the deep operational exploration surface; native panels
  cover routine checks only.
- **No notification delivery exists**: without an alert-routing component, alert state is
  informational and the console says so.
- **The interface remains loopback-bound** behind the existing key-injecting proxy; no new network
  exposure is introduced.
