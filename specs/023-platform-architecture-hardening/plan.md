# Implementation Plan: Platform Architecture Hardening & Delivery Integrity

**Branch**: `023-platform-architecture-hardening` | **Date**: 2026-07-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/023-platform-architecture-hardening/spec.md`

## Summary

Harden the post-021 single-machine architecture without adding processes or relaxing the one-GPU
invariant. The implementation proceeds as independently releasable slices: repair stale
live-evaluation topology; enforce a fail-closed internal agent credential; make offline backend/UI
quality gates reproducible and required; replace duplicated schema bootstrap with ordered SQL
migrations; give 022's registry-driven LLM activation a durable saga/reconciler; retain and bound the
stdlib agent transport; then add operation metrics, local alert rules, internal module extraction,
and current-state documentation governance.

The design deliberately uses components already resident: environment/file-based generated secrets,
the existing gateway-to-agent HTTP hop, Postgres advisory locks and tables, GitHub Actions,
Prometheus rule files, and characterization tests. It introduces no broker, ORM, alert service,
scheduler daemon, or cloud dependency.

## Technical Context

**Language/Version**: Python 3.12 for gateway/agent/tools; TypeScript 5 + React 19 on Next.js 15 for
the operator console; YAML/SQL for workflows, Compose, migrations, and alert rules.

**Primary Dependencies**: Existing FastAPI, httpx, MLflow client, psycopg 3, boto3, Prometheus client,
stdlib host-agent HTTP server, Next.js. GitHub Actions uses official checkout/setup actions. No new
runtime library is required; test-only tooling may add pinned pytest/Ruff dependencies.

**Storage**: Existing Postgres `gateway` database gains a migration ledger and, when 022 is
implemented, activation-operation state. Existing MLflow registry remains alias authority per model;
Garage remains artifact/payload storage. No new store.

**Testing**: Offline pytest and Ruff; migration tests against an ephemeral Postgres service in CI;
UI `npm ci` + production build/type-check; Compose config validation; spec consistency checks;
transport and activation failure-injection tests; existing live/hardware quickstarts for the RTX
5070 Ti.

**Target Platform**: Single Windows machine with native Ubuntu WSL GPU execution and Docker Compose
infrastructure; hosted Linux CI for dependency-light offline checks only.

**Project Type**: Existing web platform with a Python control plane, native host agent and child
runtimes, Next.js console/BFF, and local infrastructure.

**Performance Goals**:

- Authentication adds no meaningful extra network hop and sub-millisecond local comparison cost.
- Migration checks add less than 2 seconds to ordinary gateway startup when no migration is due.
- Bounded agent transport sustains the existing single-operator workload and streaming behavior.
- Agent metrics do not add more than 1% steady-state inference latency.
- CI completes dependency-light offline checks without CUDA/model installation; target ≤10 minutes
  with independent jobs.

**Constraints**:

- One GPU tenant at a time; jobs remain non-preemptable.
- No required outbound connection after initial dependency/image pulls.
- Idle infrastructure remains within the constitution's ~3 GB target.
- No new resident service or host process.
- Existing HTTP response contracts remain byte-compatible unless a new authentication failure is
  the intended outcome.
- Secrets never enter browser JavaScript or observability payloads.
- Historical specs remain immutable decision history apart from explicit errata links.

**Scale/Scope**: One operator, one gateway, one host agent, five serving modalities, four job kinds,
roughly 33k Python/TypeScript lines, 81 Python test modules, and 22 prior increments. Requirement IDs
FR-277..328, success criteria SC-152..164, tasks T490+.

## Constitution Check

*GATE: evaluated against constitution v1.5.1 before research — PASS.*

- **I. Local-First**: PASS. Agent authentication, migrations, CI definitions, reconciliation,
  metrics, and docs require no managed service. Hosted CI is a development check, not a runtime
  dependency; all commands remain runnable locally.
- **II. Single-GPU (NON-NEGOTIABLE)**: PASS. Routing uses the existing agent; authentication occurs
  before admission; activation uses existing serialized admission/swap; transport bounding rejects
  work before admission. Hardware drills explicitly verify never-two-resident behavior.
- **III. Lightweight Footprint**: PASS. No resident component is added. SQL files, workflow jobs,
  Prometheus rules, and internal modules have zero idle runtime cost. Alertmanager and heavyweight
  migration frameworks are explicitly excluded.
- **IV. Full Lifecycle**: PASS. Evaluation routing and recoverable LLM activation repair lifecycle
  links; CI and migrations improve reliability without dropping a stage.
- **V. Open-Source & Swappable**: PASS. Shared interfaces remain; no stack lock-in is introduced.
  Internal authentication and migration contracts are implementation-light and replaceable.
- **VI. Reproducibility & Observability**: PASS. Mandatory checks, migration provenance,
  activation audit records, operation metrics, and alerts directly strengthen this principle.
- **VII. Incremental Delivery**: PASS. Seven independently testable stories; US1–US3 are the MVP,
  US4 precedes 022 state, US5 integrates with 022, and later stories can land separately.

*Post-design re-check*: PASS. Contracts add one small Postgres ledger and one activation table only;
no new service, GPU tenant, cloud dependency, or constitution amendment is required.

### Constitution errata observed, not amended by 023

Principle VI still says every dataset version is recorded via DVC, while the implemented and
Principle V-described authority is the content-addressed Garage-backed dataset registry. This spec
does not silently amend the constitution; a separate governance patch should correct that stale
sentence as a wording-only update.

## Project Structure

### Documentation (this feature)

```text
docs/
└── architecture-review-2026-07-11.md

specs/023-platform-architecture-hardening/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── tasks.md
├── contracts/
│   ├── agent-security.md
│   ├── delivery-gates.md
│   ├── promotion-activation.md
│   └── schema-migrations.md
└── checklists/
    └── requirements.md
```

### Source Code (repository root)

```text
.github/workflows/
└── quality.yml                    # backend, UI, Compose, spec gates

gateway/app/
├── evaluation.py                  # canonical agent-derived live predictor URLs
├── settings.py                    # internal agent key + canonical topology
├── registry.py                    # 022 promote entry point uses activation service
├── activation.py                  # durable activation state machine/reconciler
├── repositories/                  # extracted persistence adapters (incremental)
├── scheduler.py                   # behavior preserved; external adapters extracted later
└── main.py                        # migration + reconciliation lifespan ownership

hostagent/
├── main.py                        # auth gate + bounded stdlib transport; ASGI branch removed
├── auth.py                        # constant-time internal-key policy
├── metrics.py                     # operation counters/histograms
├── lifecycle.py / swap.py         # existing invariant; reload integration only
└── asgi.py                        # removed after parity gate

platformlib/
├── topology.py                    # canonical agent URL; no legacy defaults
├── contracts.py                   # additive activation/security payloads if shared
├── store.py                       # repository facade, no embedded full-schema duplicate
├── migrations.py                  # ordered runner + compatibility checks
└── migrations/
    ├── 001_baseline.sql
    └── 002_activation.sql         # lands with 022/US5 integration

infra/
├── postgres/init.sql              # database creation only
├── prometheus/prometheus.yml      # rule_files
└── prometheus/rules/mlops-lite.yml

scripts/
├── check_specs.py                 # artifact/ID/placeholder consistency
├── migrate_db.py                  # operator-visible migration status/apply command
└── backup_gateway_db.*            # documented backup/restore helper or commands

tests/
├── test_evaluation_topology.py
├── test_agent_auth.py
├── test_agent_limits.py
├── test_migrations.py
├── test_activation.py
├── test_activation_recovery.py
└── existing regression suite

ui/
├── package.json / package-lock.json
└── existing pages/components      # desired/resident/degraded activation status under 022

README.md                          # concise current status + architecture links
requirements-dev.txt               # dependency-light offline developer/CI environment
```

**Structure Decision**: Retain the current process topology. New files are internal modules and
declarative artifacts, not new applications. `platformlib/migrations.py` is intentionally small and
Postgres-specific. `gateway/app/activation.py` owns cross-system orchestration because the gateway
already owns gated promotion and operator identity; the host agent remains the authority for actual
resident identity. Repository extraction is incremental and contract-preserving.

## Design Phases

### Phase 0 — Research

Decisions and rejected alternatives are recorded in [research.md](./research.md): trust-boundary
placement, canonical topology, GitHub Actions shape, migration ownership, activation recovery,
transport selection/bounds, metrics/alerts, and modularity limits.

### Phase 1 — Contracts and models

- Agent authentication/public-route policy: [contracts/agent-security.md](./contracts/agent-security.md)
- Required and hardware delivery gates: [contracts/delivery-gates.md](./contracts/delivery-gates.md)
- Forward migration behavior: [contracts/schema-migrations.md](./contracts/schema-migrations.md)
- Recoverable 022 activation: [contracts/promotion-activation.md](./contracts/promotion-activation.md)
- Persisted/read models and transitions: [data-model.md](./data-model.md)
- Operator and validation drills: [quickstart.md](./quickstart.md)

### Phase 2 — Tasks

[tasks.md](./tasks.md) starts at T490 and delivers the stories in priority/dependency order. Test
tasks precede implementation for correctness, security, migration, and activation changes.

## Complexity Tracking

No constitution violation requires justification.

Two additions warrant explicit simplicity notes:

| Addition | Why it is needed | Simpler alternative rejected |
|---|---|---|
| Migration ledger + small runner | Existing populated databases and 022 schema changes require ordered compatibility evidence | Continuing `CREATE/ALTER IF NOT EXISTS` cannot detect drift, checksum changes, or newer schemas |
| Activation operation + reconciler | MLflow, Postgres, and the agent cannot share a transaction | Best-effort sequential writes can silently diverge desired and resident identity after timeout/restart |
