# Phase 0 Research: Platform Architecture Hardening & Delivery Integrity

## R1. Canonical live-evaluation topology

**Decision**: Standalone evaluation code derives the base from `platformlib.topology.agent_url()` and
appends `/engines/llm` or `/engines/vision`. Explicit environment/function injection remains for
tests and specialized scoring processes.

**Rationale**: `platformlib` is already the dependency-light package shared by gateway, native host
processes, and training. Importing `gateway.app.settings` would break the deliberate
standalone-loading behavior of `evaluation.py`; retaining retired independent URL defaults repeats
the exact topology drift 018 removed.

**Rejected**:

- Restore `SERVING_URL` and `BENTO_URL` to Compose: preserves duplicated topology and masks drift.
- Route evaluation back through public gateway endpoints: introduces gateway authentication and
  logging recursion into code used by training/scoring; this can be revisited separately.
- Hard-code `:8100`: still duplicates `AGENT_PORT` and ignores injected WSL addressing.

## R2. Internal agent authentication boundary

**Decision**: Use one generated internal agent key, supplied by environment or secret file, and a
constant-time header comparison. All routes are protected except an explicit read-only probe
allow-list. Missing key fails startup unless `AGENT_ALLOW_OPEN=1` is deliberately set.

**Rationale**: The gateway API key protects the operator-facing surface, but the agent is reachable
from containers through a non-loopback bind. Because the agent owns inference, jobs, cancellation,
unload, and future reload, authorization must occur at the privileged boundary itself. One internal
key is adequate for one gateway/agent on one machine and adds no service.

**Header**: `X-Agent-Key`. `hmac.compare_digest` avoids input-dependent comparison. The gateway adds
the header on every protected hop. Approved host scripts read the same secret source. The key is not
the browser-facing `X-API-Key`.

**Public routes**: `/healthz`, `/readyz`, and `/metrics`. `/health` and `/engines` contain operational
state and should require the key unless an existing infrastructure consumer is explicitly migrated
to a reduced public shape.

**Migration**: `AGENT_CONTROL_SECRET` can be read as a one-release deprecated fallback for the new
key, but startup must warn and generated configurations must use `AGENT_API_KEY`. The even older
`SWAP_CONTROL_SECRET` must not perpetuate an implicit open mode.

**Rejected**:

- Trust source IP: WSL/container addresses change and are forgeable within the host network.
- mTLS: certificate lifecycle is disproportionate for one local hop.
- Reuse the gateway API key: expands browser/operator credential exposure and prevents independent
  rotation of external and internal boundaries.
- Authenticate control routes only: inference/job submission still permits resource exhaustion and
  bypasses gateway policy.

## R3. Reproducible delivery gates

**Decision**: Add a single workflow with independent backend, UI, Compose, and spec jobs. Add a
dependency-light `requirements-dev.txt` (or equivalent pinned extra) that installs collection and
offline-test requirements without CUDA/model runtimes. Use `npm ci` from `ui/package-lock.json`.

**Rationale**: The test suite's value depends on clean-checkout reproducibility and required PR
status. Independent jobs identify ownership and allow path-aware optimization later without hiding
failures. Hosted CI is suitable for pure/offline tests; the existing skip guards preserve honesty
for live/hardware validation.

**Backend environment**: Python 3.12, matching gateway/plan claims. Pin Ruff and pytest. Include the
lightweight gateway/store/agent dependencies needed for collection. Tests that need optional model
libraries must stub/import lazily or move to the hardware gate; CI must not install torch/CUDA.

**UI environment**: a pinned Node LTS recorded in the workflow/project metadata; always run
`npm ci` in the CI filesystem rather than reusing host/WSL `node_modules`. `next build` is the
authoritative type/build gate. Add a supported lint command if `next lint` is not valid for the
pinned Next version.

**Compose validation**: create an ephemeral CI env file with syntactically valid non-production
values and run `docker compose config --quiet`; do not start services.

**Spec validation**: repository script checks expected artifact files, placeholders, duplicate IDs,
task numbering/format, requirement-to-task references, and that a spec-only PR does not mark
implementation tasks complete.

**Rejected**:

- Run only the current smoke target: covers one foundation path, not the architecture.
- Install `training/requirements.txt` and CUDA wheels: slow, disk-heavy, and unnecessary for offline
  behavior.
- Treat AI code review as CI: review and executable verification solve different problems.

## R4. Schema migration ownership and format

**Decision**: A small Postgres-specific runner applies ordered SQL files and records
`version/checksum/applied_at`. The gateway is the normal migration owner. Other processes perform a
compatibility check. A Postgres advisory lock serializes startup.

**Rationale**: The schema is small and SQL-first, so Alembic/ORM adoption would add machinery without
value. Plain numbered SQL preserves reviewability and works with psycopg already installed. One
runner for empty and populated databases eliminates drift between `init.sql` and runtime bootstrap.

**Baseline strategy**:

- `001_baseline.sql` describes the existing post-018 schema.
- Existing installations without a ledger are inspected for the known baseline, then stamped `001`
  only when the expected tables/columns/constraints match.
- Fresh databases apply `001` normally.
- Later schema changes, including 022 activation state, receive new files.

**Transaction/lock**: Acquire a fixed advisory lock, read ledger, verify checksums, and apply each
migration in its own transaction. Statements that cannot run transactionally must be explicitly
declared and are not expected in 023.

**Compatibility**: binaries declare minimum/current schema. Newer database versions fail closed for
writes. Read-only health may report the mismatch.

**Rollback**: forward-fix by default. Destructive evolution uses expand → dual-read/write if needed
→ contract in a later release. The quickstart requires a tested `pg_dump`/restore before migrating
populated state.

**Rejected**:

- Continue idempotent bootstrap DDL: cannot prove order, detect edited history, or gate compatibility.
- Let gateway and agent independently mutate schema: unnecessary race and unclear ownership.
- Add Alembic: no ORM, small schema, and a constitution preference for minimal dependencies.

## R5. Recoverable 022 LLM activation

**Decision**: Model promotion as a durable state machine coordinated by the gateway, with an
operation record, per-platform serialization, idempotent steps, honest desired/resident identity,
and startup/periodic reconciliation.

**Rationale**: MLflow alias state, the Postgres active-model pointer, and the agent's resident child
cannot commit atomically. A saga-style operation makes partial progress visible and recoverable
without a broker. The gateway already owns gated promotion, operator authentication, and policy
semantics; the agent alone owns actual resident identity.

**Sequence**:

1. Validate gate verdict, operator-only rule, registry descriptors, base/adapter artifacts, agent
   availability, and admission/preemption conditions without evicting the current model.
2. Acquire the activation serialization lock and create/reuse the operation (`prepared`).
3. Record target desired state and move the target model's `@serving` alias (`committing`).
4. Request idempotent controlled reload keyed by operation ID (`reloading`).
5. Read agent-reported `model_name + version`; only a match permits `active`.
6. On failure, attempt to restore prior pointer/alias and ensure prior resident (`rolling_back`).
7. Mark `rolled_back` after verification or `degraded` with exact mismatch/error.

**Ordering note**: The pointer may temporarily name the desired target while the old model remains
resident. All UI and logging must show desired and resident separately; predictions use resident
identity only. No step may infer actual residency from MLflow or Postgres.

**Reconciliation**: On gateway startup and periodically while non-terminal operations exist, compare
operation, pointer, target/previous aliases, and agent identity. Reissue only idempotent commands.
Operator intervention can retry or explicitly choose rollback; it cannot mark an operation active
without resident verification.

**Rejected**:

- Best-effort sequential calls: timeout ambiguity creates silent divergence.
- Two-phase commit: MLflow and the native agent do not participate in a common transaction.
- Make MLflow alias the sole cross-name authority: aliases are scoped per registered model; 022 needs
  one platform-wide active LLM selection.
- Auto-switch on policy green: explicitly deferred by 022 governance.

## R6. Agent transport consolidation and bounds

**Decision**: Retain the stdlib server selected by 020 hardware validation, wrap it with a bounded
worker/queue policy, impose endpoint-specific body limits and timeouts, add graceful shutdown, then
delete the uvicorn/ASGI path after parity tests.

**Rationale**: The transport-neutral router was valuable for the 020 comparison, but permanent dual
transport multiplies SSE and error-mapping surface. The stdlib server keeps the default agent free
of framework dependencies. Single-operator throughput is modest; deterministic bounds matter more
than peak concurrency.

**Initial bounds to validate**:

- Worker limit: 16 total request workers.
- Pending accept/work queue: 32.
- JSON request body: 1 MiB.
- Multipart image/audio body: configurable, default 32 MiB, aligned with supported local inputs.
- Header/read timeout: 15 s; ordinary write timeout: 30 s; inference-specific downstream timeouts
  remain endpoint aware.
- Graceful shutdown: stop accepts, drain for 30 s, then use existing child cleanup semantics.

Exact values are configuration defaults, not hard requirements; target-hardware drills may lower
them. Chunked request bodies must count bytes while reading and stop at the same limit.

**Rejected**:

- Keep both transports indefinitely: no longer serves an experiment and doubles contract surface.
- Switch to uvicorn despite the measured verdict: adds a dependency and changes the selected
  operational baseline without new evidence.
- Rely on local-only intent instead of limits: the agent binds beyond loopback for container reach.

## R7. Metrics and alerting without another service

**Decision**: Extend existing Prometheus exposition and load rule files into the current Prometheus
container. Grafana shows active rules/conditions and links to markdown runbooks. Do not add
Alertmanager.

**Rationale**: Local actionability requires detection, not paging infrastructure. Counters and
histograms answer whether admission, reload, jobs, scheduling, migrations, and stores are succeeding;
current gauges alone cannot. Prometheus already exists and directly scrapes the agent.

**Cardinality rules**: labels are fixed vocabularies (`route_group`, `method`, `outcome`, `kind`,
`engine` from the bounded registry). Never label by model name, job ID, prediction ID, error string,
dataset, or operation ID.

**Rejected**:

- Alertmanager/email/Slack: no notification requirement and another resident/credentialed service.
- Logs only: poor detection and trend visibility.
- Per-model metric labels: unbounded registry growth risks Prometheus cardinality.

## R8. Modularity boundary

**Decision**: Split large modules only behind characterization tests and only within existing
processes. Prioritize store repositories, activation state machine, evaluation predictors/metrics,
agent router/transport, scheduler adapters/state transitions, and UI hooks/panels.

**Rationale**: File size is not itself a defect, but current hotspots mix reasons to change. Clear
internal interfaces improve reviewability without violating the platform's lightweight design.

**Rejected**:

- New microservices: increases resident footprint, topology, authentication, and failure modes.
- Repository-wide rewrite: breaks incremental phase gates and obscures behavior changes.
- Mechanical line-count limits: encourage arbitrary fragmentation rather than coherent ownership.

## R9. Current-state documentation governance

**Decision**: Keep one dated architecture baseline linked from a concise README. Specs remain
append-only decision history. A checklist verifies topology, data ownership, trust boundaries,
status, commands, and current ports whenever they change.

**Rationale**: The long spec history is valuable but unsuitable as the operator's source of truth.
The README currently mixes both roles and has contradictory status statements. Separating present
state from history reduces accidental use of retired topology without deleting evidence.

**Rejected**:

- Rewrite old specs: destroys decision provenance.
- Generate all docs from code: architecture rationale and failure semantics are not fully derivable.
- Keep the README as an exhaustive changelog: it will continue to drift and overwhelm onboarding.
