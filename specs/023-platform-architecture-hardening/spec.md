# Feature Specification: Platform Architecture Hardening & Delivery Integrity

**Feature Branch**: `023-platform-architecture-hardening`
**Created**: 2026-07-11
**Status**: Draft
**Input**: Full post-021 architecture review and recommendations, requested as a GitHub Spec Kit
brownfield increment.

## Overview

MLOps-Lite's 018–021 increments successfully consolidated GPU ownership, made operational state
durable, closed the policy loop, replaced archived dependencies, and made the console lifecycle
native. The resulting single-box architecture is appropriate and should not be redesigned into a
distributed platform. The review nevertheless found three immediate integrity gaps and several
next-stage hardening needs:

1. live evaluation still defaults to retired pre-018 daemon ports;
2. direct host-agent access can bypass the gateway's fail-closed security boundary;
3. extensive backend tests and the UI build are not enforced by CI or a reproducible developer
   dependency setup;
4. the relational schema has duplicated bootstrap DDL rather than a migration history;
5. 022's future LLM go-live action spans three systems without an explicit partial-failure model;
6. the agent retains two transports and unbounded request edges after the hardware verdict selected
   one;
7. operation metrics, alert rules, module boundaries, and current-state documentation lag the
   platform's maturity.

023 hardens these boundaries without adding a resident service, changing the one-GPU rule, or
expanding beyond one machine and one operator. Each user story is independently shippable; the first
three form the MVP. The 022 recovery story is a prerequisite for declaring registry-driven LLM
activation complete, but it does not otherwise block the hardening MVP.

## Clarifications

### Session 2026-07-11

- **Scope**: This is an architecture-hardening increment, not a topology redesign. Existing process
  boundaries remain: one gateway, one host agent, child runtimes, Postgres, Garage, MLflow,
  Prometheus/Grafana, and the native UI/supervisor.
- **Agent authentication**: The agent becomes fail-closed for inference, job, and control routes.
  Health/readiness and Prometheus metrics remain available to local infrastructure. A separate,
  explicit development override may open the agent and must log a prominent warning.
- **Migration posture**: Use a lightweight ordered SQL runner, not an ORM or new resident migration
  service. Migrations are forward-only and expand/contract by policy.
- **022 relationship**: 022 continues to own registry-driven LLM selection and UI behavior. 023 owns
  the cross-system activation/recovery contract that 022 must use.
- **Transport**: Keep the stdlib agent transport selected by the 020 on-hardware comparison; remove
  the ASGI alternative after parity checks.
- **Delivery**: Offline CI is required. GPU/live-stack checks remain explicit target-hardware gates
  and must never cause CI to download models or install the CUDA training stack.
- **Observability**: Prometheus rules and Grafana surfaces are in scope. A notification service such
  as Alertmanager is not.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Correct internal routing after consolidation (Priority: P1)

As an operator evaluating a serving model, I need live evaluation to use the same consolidated host
agent topology as ordinary inference so a healthy platform does not fail because code still points
at deleted daemons.

**Why this priority**: This is a current correctness defect in a promotion-gating path. A failed or
misrouted evaluation undermines the registry and promotion workflow.

**Independent Test**: With only `AGENT_URL` configured, run LLM and vision live-predictor tests and
verify requests target `/engines/llm` and `/engines/vision` on the agent. Scan executable routing
code and verify retired service ports are absent.

**Acceptance Scenarios**:

1. **Given** the consolidated agent is configured and no legacy `SERVING_URL` is set, **When** an LLM
   live evaluation runs, **Then** every inference request reaches `${AGENT_URL}/engines/llm/infer`.
2. **Given** the consolidated agent is configured and no legacy `BENTO_URL` is set, **When** a vision
   live evaluation runs, **Then** every classification request reaches
   `${AGENT_URL}/engines/vision/classify`.
3. **Given** a test explicitly injects a predictor endpoint, **When** evaluation runs, **Then** the
   injected endpoint remains usable without importing the gateway application.
4. **Given** the post-018 codebase, **When** the topology guard runs, **Then** it fails if executable
   routing code reintroduces retired daemon ports.

---

### User Story 2 — Enforce the internal agent trust boundary (Priority: P1)

As an operator, I need the host agent to reject unauthorized inference, job, and lifecycle requests
so callers cannot bypass gateway authentication or manipulate the GPU execution plane directly.

**Why this priority**: The host agent can load/unload models, consume GPU time, launch training, and
cancel jobs. Its current optional secret makes the most privileged local API fail-open.

**Independent Test**: Start the agent with a configured internal key, call every protected route
without/with a wrong key and receive 401/403, then call with the correct key and preserve existing
behavior. Verify health/readiness/metrics remain scrapeable and missing production configuration
prevents startup.

**Acceptance Scenarios**:

1. **Given** no internal key and no development override, **When** the agent starts, **Then** startup
   fails before binding a listening socket with a clear remediation message.
2. **Given** a configured key, **When** an unauthorized caller submits inference or a job, **Then**
   the request is rejected before admission, child startup, journal mutation, or database writes.
3. **Given** a configured key, **When** the gateway forwards an authorized operation, **Then** it
   injects the internal key and the existing response contract is preserved.
4. **Given** Prometheus or a readiness probe, **When** it reads an explicitly public probe route,
   **Then** no credential is required and no sensitive state beyond the documented probe contract is
   returned.
5. **Given** `AGENT_ALLOW_OPEN=1` in a throwaway environment, **When** the agent starts without a key,
   **Then** it starts open and emits an unmistakable warning without logging credential values.
6. **Given** an authorized or rejected request, **When** logs and metrics are inspected, **Then** no
   secret value appears.

---

### User Story 3 — Make quality gates reproducible and mandatory (Priority: P1)

As a maintainer, I need one documented setup and required pull-request checks so the repository's
tests, lint rules, UI type-check/build, and Compose configuration protect every change.

**Why this priority**: The repository has broad tests, but no CI workflow runs them. Developers can
reach collection/build failures because required tools are distributed across runtime-specific
files and cross-shell installs.

**Independent Test**: From a clean checkout on a standard GitHub runner, execute the documented
bootstrap and complete all offline checks without GPU access, model downloads, live services, or
manual dependency discovery.

**Acceptance Scenarios**:

1. **Given** a clean checkout, **When** the documented backend development setup runs, **Then** all
   dependencies required for collection and offline tests are installed in one command.
2. **Given** a pull request, **When** backend code or specifications change, **Then** Ruff, offline
   pytest, and spec consistency checks run and report independently.
3. **Given** a pull request affecting the console or shared contracts, **When** UI checks run, **Then**
   dependencies are installed with the lockfile and the production build/type-check succeeds.
4. **Given** the Compose configuration, **When** CI validates it, **Then** missing required variables
   are supplied only as non-production test values and no service is started.
5. **Given** tests marked live or hardware-only, **When** ordinary CI runs, **Then** they skip with an
   explicit reason and CI does not install CUDA packages or download model weights.
6. **Given** any required check fails, **When** branch protection evaluates the PR, **Then** the
   change cannot be considered implementation-complete.

---

### User Story 4 — Evolve operational data through one migration history (Priority: P2)

As an operator upgrading an existing installation, I need deterministic, idempotent schema
migrations so new binaries either upgrade the `gateway` database safely or refuse an incompatible
schema without corrupting state.

**Why this priority**: 022 will add new serving state, while the current bootstrap uses duplicated
DDL and a version value that does not track applied changes.

**Independent Test**: Apply the migration runner to an empty database and a version-1 fixture,
verify identical final schemas and preserved rows, rerun as a no-op, and prove concurrent runners
apply each version once.

**Acceptance Scenarios**:

1. **Given** a fresh `gateway` database, **When** migrations run, **Then** the complete current schema
   is created and every applied version/checksum is recorded once.
2. **Given** an existing version-1 database with predictions, jobs, and policies, **When** migrations
   run, **Then** rows are preserved and only unapplied forward migrations execute.
3. **Given** gateway and another process start concurrently, **When** both check schema state,
   **Then** a database lock serializes migration and no statement is applied twice.
4. **Given** a migration file changes after application, **When** compatibility is checked, **Then**
   checksum mismatch fails loudly.
5. **Given** a database newer than the running binary supports, **When** the binary starts, **Then**
   it refuses write operations with a clear compatibility error.
6. **Given** the first migration of populated state, **When** the operator follows the quickstart,
   **Then** a backup and restore verification occurs before upgrade.

---

### User Story 5 — Recover registry-driven LLM activation (Priority: P2)

As an operator promoting an LLM under 022, I need the registry alias, active-model pointer, and
actually resident model to converge after timeouts, restarts, or partial failures so the platform
never silently reports a model that is not serving.

**Why this priority**: Promotion spans MLflow, Postgres, and the host agent. Without a durable
operation model, no implementation can make the cross-system action atomic.

**Independent Test**: Inject a failure after each activation step, restart the gateway/agent as
applicable, run reconciliation, and verify convergence to either the new active model or the
recorded previous model with honest degraded status throughout.

**Acceptance Scenarios**:

1. **Given** a valid target, **When** operator promotion succeeds, **Then** one activation operation
   reaches `active`, the alias and pointer identify the target, and the agent reports that target as
   resident.
2. **Given** an unavailable artifact or non-preemptable job, **When** activation is prepared, **Then**
   mutation is refused/deferred before a working serving model is evicted.
3. **Given** a timeout after a state-changing call, **When** the request is retried with the same
   operation key, **Then** no duplicate activation or reload occurs.
4. **Given** the pointer changes but reload fails, **When** recovery executes, **Then** it restores the
   previous desired state or records a degraded operation requiring retry; it never labels the old
   resident model as the target.
5. **Given** the gateway restarts with an incomplete operation, **When** reconciliation starts, **Then**
   it compares the operation, alias, pointer, and agent-reported resident identity and safely
   completes or rolls back.
6. **Given** rapid successive operator switches, **When** they are processed, **Then** operations are
   serialized, the last accepted request wins, and the one-GPU invariant is maintained.
7. **Given** an automated policy promotes a candidate, **When** the model is text-generation, **Then**
   022's operator-only live-switch boundary remains enforced.

---

### User Story 6 — Bound and simplify the host-agent transport (Priority: P2)

As an operator, I need the agent to use one measured transport with bounded resource consumption so
malformed or excessive local requests cannot exhaust memory or threads and transport behavior has
one implementation.

**Why this priority**: The platform retained both stdlib and ASGI transports after choosing stdlib,
while the default handler trusts request body length and uses unbounded request threads.

**Independent Test**: Run transport parity fixtures against the retained server, then prove
oversized bodies, excess concurrency, slow reads, disconnects, and shutdown are bounded without
breaking SSE or the GPU admission contract.

**Acceptance Scenarios**:

1. **Given** a request larger than its endpoint limit, **When** headers/body are received, **Then** it
   is rejected with 413 before the full body is held in memory or domain logic runs.
2. **Given** more simultaneous requests than the configured worker bound, **When** capacity is
   exhausted, **Then** excess work waits within a bounded queue or receives 503; no unbounded thread
   growth occurs.
3. **Given** a slow or disconnected client, **When** read/write timeouts expire, **Then** resources
   and engine locks are released safely.
4. **Given** a streaming inference, **When** the retained transport serves it, **Then** the stream
   preserves first-frame error mapping and holds one runtime lock end-to-end.
5. **Given** transport parity is proven on hardware, **When** cleanup completes, **Then** the ASGI
   runtime switch, code path, dependency rationale, and duplicate tests are removed.
6. **Given** agent shutdown, **When** active requests drain within the configured grace period,
   **Then** child cleanup and journal semantics remain intact.

---

### User Story 7 — Make failures actionable and architecture current (Priority: P3)

As an operator and maintainer, I need actionable local alerts, comprehensible internal modules, and
one current architecture reference so failures and design changes can be understood without reading
the entire increment history.

**Why this priority**: Direct agent metrics and extensive specs exist, but alert rules are absent,
operation outcomes are incomplete, large modules are change hotspots, and present-tense docs retain
stale transition status.

**Independent Test**: Trigger synthetic failure metrics and validate Prometheus rules; run
characterization tests before/after module extraction; verify README/current architecture facts
against executable configuration and a documentation checklist.

**Acceptance Scenarios**:

1. **Given** a wedged engine, repeated scheduler failure, migration failure, unavailable state store,
   or low disk condition, **When** Prometheus evaluates rules, **Then** an actionable local alert is
   visible with the affected component and remediation link.
2. **Given** admission, swap/reload, request, job, scheduler, database, or object-store operations,
   **When** they complete, **Then** bounded-cardinality counters and latency metrics expose outcomes.
3. **Given** a large production module selected for extraction, **When** characterization tests pass,
   **Then** repositories/adapters/state machines can be separated without changing public APIs,
   processes, or behavior.
4. **Given** an implementation increment changes topology, data authority, security boundaries, or
   runtime status, **When** its checklist is completed, **Then** README and the current architecture
   reference are updated in the same PR.
5. **Given** an operator reads the README, **When** they follow its architecture links, **Then** they
   see current components and status while historical specs remain clearly labeled as history.

### Edge Cases

- An agent key is rotated while the gateway has persistent HTTP connections.
- Prometheus probes the agent during key misconfiguration; public metrics remain available without
  exposing control or inference routes.
- A legacy host sets `SWAP_CONTROL_SECRET` but not the new internal key.
- CI runs from Windows, Linux, or a WSL checkout with incompatible pre-existing `node_modules`.
- A migration succeeds but the process dies before recording completion, or records completion but
  the transaction rolls back.
- Two gateway starts contend for the migration advisory lock.
- An activation request times out after the agent loaded the target but before the gateway records
  `active`.
- MLflow is reachable while Postgres activation state is unavailable, or the reverse.
- Rollback restores the pointer but cannot restore the MLflow alias.
- A second activation is requested while the first is incomplete.
- An oversized multipart request omits `Content-Length` or uses chunked transfer.
- A streaming client disconnects before the first frame or during generation.
- Metrics labels include user/model supplied values that could create unbounded cardinality.
- Historical specs intentionally describe retired topology and must not be rewritten as current
  documentation.

## Requirements *(mandatory)*

### Functional Requirements

#### Routing integrity

- **FR-277**: Every executable live-evaluation predictor MUST derive its default endpoint from the
  canonical consolidated agent topology.
- **FR-278**: LLM live evaluation MUST target `/engines/llm/infer`; vision live evaluation MUST
  target `/engines/vision/classify`.
- **FR-279**: Evaluation MUST remain standalone-loadable by training/scoring code and MUST preserve
  explicit predictor injection for tests.
- **FR-280**: An automated guard MUST fail if retired modality-daemon ports are reintroduced into
  executable request routing; documentation and historical comments are exempt.

#### Agent trust boundary

- **FR-281**: The host agent MUST require a generated internal credential for every inference, job
  submission/status/cancellation, engine-control, reload, and other state-changing route.
- **FR-282**: Authentication MUST occur before admission, child lifecycle, journal, database, or
  object-store side effects.
- **FR-283**: Health, readiness, and metrics routes MAY remain unauthenticated only through an
  explicit public-route allow-list with documented response shapes.
- **FR-284**: Missing agent credentials MUST prevent normal startup unless an explicit
  `AGENT_ALLOW_OPEN` development override is enabled.
- **FR-285**: Open-development mode MUST emit a prominent warning and MUST never be the shipped or
  generated default.
- **FR-286**: The gateway and approved host-side tools MUST inject the agent credential without
  exposing it to browser code, logs, metrics, command arguments, or response bodies.
- **FR-287**: Credential comparison MUST use constant-time comparison, and rejected requests MUST
  use a stable 401/403 error contract without revealing configuration state.
- **FR-288**: Existing `AGENT_CONTROL_SECRET`/`SWAP_CONTROL_SECRET` installations MUST receive an
  explicit migration path; compatibility MUST NOT silently retain fail-open behavior.

#### Delivery integrity

- **FR-289**: The repository MUST provide one documented command sequence that installs all
  dependencies needed to collect and run the offline backend suite from a clean checkout.
- **FR-290**: Pull requests MUST run Ruff and the offline pytest suite on the repository's supported
  Python version.
- **FR-291**: Pull requests MUST install UI dependencies from `package-lock.json` and run the
  production build/type-check on the supported Node version.
- **FR-292**: Pull requests MUST validate Docker Compose configuration without starting services.
- **FR-293**: Live-stack, GPU, and hardware tests MUST be marked separately, skip explicitly in
  ordinary CI, and have a documented target-hardware gate.
- **FR-294**: Ordinary CI MUST NOT install the CUDA training stack, start the platform, download
  model weights, or require repository secrets.
- **FR-295**: Backend, UI, Compose, and specification checks MUST report as distinct jobs so failures
  identify their owning surface.
- **FR-296**: The full spec set MUST have an automated consistency check for required files,
  unresolved placeholders, unique requirement/task IDs, and unchecked implementation status.

#### Schema evolution

- **FR-297**: The relational store MUST use ordered, forward-only migration files as the single
  source of schema truth for fresh databases and upgrades.
- **FR-298**: Each applied migration MUST record a unique version, immutable checksum, and applied
  timestamp in a migration ledger.
- **FR-299**: Migration application MUST be transactionally safe where Postgres permits and MUST be
  serialized with a database advisory lock.
- **FR-300**: Re-running migrations MUST be idempotent; an applied-version checksum mismatch MUST
  fail loudly.
- **FR-301**: A binary MUST refuse writes when the database schema is newer than it supports or when
  required migrations are missing and cannot be applied.
- **FR-302**: `infra/postgres/init.sql` MUST stop duplicating application schema and retain only the
  database/bootstrap responsibilities unavailable to the application connection.
- **FR-303**: Destructive changes MUST use an expand/contract sequence and MUST NOT rely on automatic
  down migrations.
- **FR-304**: The upgrade quickstart MUST require and verify a restorable backup before the first
  migration against populated state.

#### Recoverable LLM activation

- **FR-305**: Each operator-initiated 022 LLM activation MUST have a durable, unique operation record
  containing target identity, previous identity, actor, timestamps, state, attempt count, and last
  error.
- **FR-306**: Activation MUST serialize per platform so two operations cannot mutate the serving LLM
  concurrently.
- **FR-307**: Target registry metadata, local artifacts, base/adapter compatibility, admission
  eligibility, and agent availability MUST be validated before evicting a working model.
- **FR-308**: Alias update, active pointer update, reload request, resident verification, rollback,
  and reconciliation MUST be individually idempotent.
- **FR-309**: The operation MUST distinguish desired identity from agent-reported resident identity;
  prediction logging MUST always use the latter.
- **FR-310**: An incomplete activation MUST reconcile after gateway or agent restart by reading the
  operation, MLflow alias, active pointer, and agent-reported identity.
- **FR-311**: Failed activation MUST preserve or restore the previous working resident when safe; if
  convergence cannot be established, the platform MUST expose a degraded state and actionable error.
- **FR-312**: Retrying the same idempotency key MUST return the existing operation and MUST NOT launch
  another reload.
- **FR-313**: Automated policy promotion MUST NOT activate a text-generation model in 023; live LLM
  switching remains operator-only as specified by 022.
- **FR-314**: Activation audit history MUST remain queryable after completion and MUST identify
  success, rollback, degraded, and operator intervention outcomes.

#### Bounded single transport

- **FR-315**: The host agent MUST retain one production HTTP transport after parity validation; the
  unselected transport and runtime switch MUST be removed.
- **FR-316**: Agent request concurrency and pending work MUST be bounded by configuration with safe
  defaults for one local operator.
- **FR-317**: JSON, multipart, and any streamed/chunked request bodies MUST have explicit endpoint
  limits and MUST reject excess data with 413 before unbounded buffering.
- **FR-318**: Read, write, child-probe, and graceful-shutdown timeouts MUST be explicit and tested.
- **FR-319**: SSE MUST preserve pre-header error mapping, one-thread generator ownership, disconnect
  cleanup, and the engine lock across generation.
- **FR-320**: Transport saturation MUST fail predictably without acquiring GPU admission for work
  that cannot be serviced.

#### Operability, modularity, and documentation

- **FR-321**: Agent and gateway metrics MUST expose bounded-cardinality outcome counters and latency
  distributions for requests, admission, swap/reload, jobs, scheduler actions, database operations,
  and object-store operations.
- **FR-322**: Prometheus MUST load local alert rules for wedged engines, prolonged GPU holds,
  repeated scheduler/activation/migration failures, low disk, and unavailable durable stores.
- **FR-323**: Every alert MUST include an operator-facing summary and remediation/runbook reference;
  no notification service is required.
- **FR-324**: Internal refactors MUST begin with characterization tests and MUST preserve public APIs,
  process boundaries, stored contracts, and the one-GPU invariant.
- **FR-325**: Persistence, state-machine, external-adapter, transport, and UI orchestration concerns
  SHOULD be separated when a production module is refactored; no new resident service may result.
- **FR-326**: The README MUST identify the actual merged/specification status and link one
  authoritative current-state architecture document.
- **FR-327**: Historical specs and reviews MUST remain available and MUST be labeled as historical
  rather than rewritten to imply current topology.
- **FR-328**: Every future increment changing topology, data authority, trust boundaries, or runtime
  status MUST include current-documentation updates in its acceptance checklist.

### Key Entities

- **AgentCredentialPolicy**: Internal authentication configuration: key source, public-route
  allow-list, open-development override, and rotation/migration status.
- **SchemaMigration**: One immutable ordered schema change with version, checksum, description, and
  application timestamp.
- **ActivationOperation**: Durable cross-system LLM activation record containing previous/target
  identities, state, attempts, actor, errors, and reconciliation evidence.
- **ServingIdentity**: Desired registry target and actual agent-reported resident model/version; the
  two are deliberately distinct while activation is incomplete.
- **DeliveryGate**: One independently reported CI result with trigger paths, command, environment,
  and required/optional status.
- **AlertRule**: Local Prometheus condition with bounded labels, duration, severity, summary, and
  remediation reference.
- **ArchitectureBaseline**: Current component/data/trust-boundary record linked from README and
  updated when those facts change.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-152**: With only `AGENT_URL` set, 100% of LLM and vision live-evaluation requests in contract
  tests target the consolidated agent paths; zero executable defaults reference retired ports.
- **SC-153**: Every protected agent route rejects missing and incorrect credentials before any
  observable side effect; 100% of explicitly public probe routes remain available.
- **SC-154**: A clean GitHub runner completes all required offline backend, UI, Compose, and spec
  jobs from documented commands with no GPU, model download, live service, or secret.
- **SC-155**: The entire offline Python suite collects successfully in the declared CI environment;
  live/hardware skips identify their reason.
- **SC-156**: Fresh-schema and version-1-upgrade tests produce the same expected schema; a repeated
  migration run applies zero changes and preserves all fixture rows.
- **SC-157**: A concurrency test with at least two migration runners records every migration exactly
  once; altered checksums and newer unsupported schemas fail closed.
- **SC-158**: Failure injection after every LLM activation step converges after reconciliation to
  either the verified target or verified previous model, with no falsely reported resident identity
  and no duplicate reload.
- **SC-159**: A 100-cycle rapid-switch hardware drill observes at most one GPU tenant at all times
  and converges to the last accepted target.
- **SC-160**: Oversized-body and saturation tests keep agent memory/thread counts within configured
  bounds and return stable 413/503 outcomes without acquiring admission.
- **SC-161**: The retained agent transport passes existing REST, SSE, lifecycle, swap, and job
  contract tests plus the target-hardware stream drill; the alternate transport is absent.
- **SC-162**: Synthetic inputs fire every new Prometheus rule within its documented evaluation
  window and each alert links a remediation section.
- **SC-163**: Refactored hotspots show no public-contract diff and all characterization/regression
  tests pass; resident services and idle resource budget remain unchanged.
- **SC-164**: README, current architecture documentation, Compose services, topology registry, and
  merged/spec status have zero factual conflicts under the documentation checklist.

## Assumptions

- The platform remains single-machine and single-operator; the gateway and scheduler remain
  single-replica.
- Docker Compose infrastructure and native WSL host execution remain the supported hybrid topology.
- Existing gateway API-key and BFF protections remain and are separate from the internal agent key.
- Postgres 17 supports the required transactional DDL and advisory-lock behavior.
- The gateway is the migration owner during normal bring-up; host processes verify compatibility
  rather than independently racing to evolve schema.
- The 020 hardware result selecting stdlib remains valid unless parity validation uncovers a
  correctness regression.
- 022 is implemented after or alongside the migration and activation-recovery foundations defined
  here.
- Required GitHub branch-protection configuration may be applied outside the repository, but the
  named check-producing workflows live in the repository.

## Dependencies

- 018 host agent, shared contracts, policy loop, and relational store.
- 019 lifecycle/admission remediation guarantees.
- 020 Garage and slim-child runtime, plus the stdlib transport hardware verdict.
- 021 BFF allow-list and lifecycle-native console.
- 022 for the LLM selection and UI behavior consumed by User Story 5; 023's other stories do not
  depend on 022 implementation.

## Non-Goals

- Multi-node, multi-user, or multi-replica operation.
- Kubernetes, Redis, a message broker, distributed transactions, or distributed locks.
- A new resident migration, scheduler, alerting, or secrets service.
- Concurrent GPU residency or relaxation of job non-preemption.
- Automatic policy-driven live LLM switching.
- Replacing MLflow, Garage, Postgres, Prometheus, Grafana, FastAPI, or llama.cpp.
- A full ORM adoption or general repository rewrite.
- Rewriting historical specifications to match current topology.
- Installing GPU libraries or running target-hardware validation in hosted CI.
