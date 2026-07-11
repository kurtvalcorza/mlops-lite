# Phase 1 Data Model: Platform Architecture Hardening & Delivery Integrity

023 adds two small persisted control records and several non-persisted contract/read models. It does
not move existing business data or introduce another database.

## Persisted entities

### SchemaMigration

The migration ledger is the authority for application schema history.

| Field | Type | Constraints | Meaning |
|---|---|---|---|
| `version` | integer | primary key, positive, immutable | Ordered migration number |
| `name` | text | non-empty, immutable | Human-readable file/name |
| `checksum` | text | SHA-256, immutable | Digest of exact applied SQL bytes |
| `applied_at` | timestamptz | non-null | Database time of successful commit |
| `applied_by` | text | non-empty | Binary/tool identity for audit |
| `duration_ms` | integer | non-negative | Application duration |

**Invariants**:

- Versions are strictly increasing and never reused.
- An applied migration's bytes never change; checksum mismatch is an error.
- A version becomes visible only in the same successful transaction as its schema changes.
- The runner holds the platform's migration advisory lock while inspecting/applying history.

### ActivationOperation

Created only when 022's registry-driven LLM activation is implemented. It makes one operator
go-live action recoverable across Postgres, MLflow, and the host agent.

| Field | Type | Constraints | Meaning |
|---|---|---|---|
| `operation_id` | UUID/text | primary key | Stable idempotency key |
| `state` | enum text | non-null | Current state below |
| `actor` | text | non-empty | Operator identity/source |
| `target_model` | text | non-empty | Desired registry model name |
| `target_version` | text | non-empty | Desired registry version |
| `previous_model` | text nullable | paired with previous version | Prior desired/verified model |
| `previous_version` | text nullable | paired with previous model | Prior desired/verified version |
| `attempts` | integer | ≥0 | Idempotent command/reconcile attempts |
| `created_at` | timestamptz | non-null | Operation creation |
| `updated_at` | timestamptz | non-null | Latest durable transition |
| `last_error_code` | text nullable | bounded vocabulary | Machine-actionable failure category |
| `last_error` | text nullable | bounded length | Operator-readable sanitized detail |
| `evidence` | jsonb | object | Alias/pointer/resident observations, no secrets |

**States**:

```text
prepared
  └─> committing
        └─> reloading
              ├─> active          (terminal success)
              └─> rolling_back
                    ├─> rolled_back (terminal safe failure)
                    └─> degraded    (terminal/manual or retryable intervention)

prepared/committing/reloading --restart/reconcile--> same or next safe state
```

**Invariants**:

- At most one non-terminal activation exists platform-wide.
- Reusing `operation_id` returns/reconciles the same operation.
- `active` requires agent-reported resident identity equal to target.
- `rolled_back` requires pointer/alias/resident verification against previous identity where a
  previous identity exists.
- `degraded` never masquerades as success and carries enough evidence for operator action.
- Prediction identity is never read from this table; it comes from the agent at request time.
- Secrets, raw prompts, artifacts, and arbitrary exception representations are forbidden.

## Configuration/contract entities

### AgentCredentialPolicy

Runtime configuration, not a database row.

| Field | Meaning |
|---|---|
| `key_source` | Environment or permission-restricted file containing the internal key |
| `allow_open` | Explicit development-only override, false by default |
| `public_routes` | Exact read-only probe paths exempt from the key |
| `deprecated_source_used` | Warning/audit flag during one-release migration |

**Validation**: key is non-empty and meets generated-secret entropy/length rules; values are never
serialized in health, errors, logs, or metrics.

### DeliveryGate

Declarative CI/readme model.

| Field | Meaning |
|---|---|
| `name` | Stable branch-protection check name |
| `surface` | backend, UI, Compose, specs, or hardware |
| `command` | Exact locally reproducible command sequence |
| `environment` | Supported runtime and dependency source |
| `required` | Required PR check vs recorded manual hardware gate |
| `artifacts` | Test/build reports retained for diagnosis |

### ServingIdentity

A read model that prevents desired/actual confusion during activation.

| Field | Authority | Meaning |
|---|---|---|
| `desired_model/version` | Active pointer + target alias | Model the platform is trying to activate |
| `resident_model/version` | Host agent | Model actually loaded and serving |
| `activation_operation_id` | Postgres | Current/recent operation, when any |
| `activation_state` | Postgres | active/reloading/rolling_back/degraded/etc. |
| `consistent` | Derived | Desired and resident identities match in an allowed terminal state |

### AlertRule

Declarative Prometheus rule with fixed-cardinality labels.

| Field | Meaning |
|---|---|
| `alert` | Stable rule name |
| `expr` | PromQL condition |
| `for` | Persistence window that filters transient states |
| `severity` | Fixed local severity vocabulary |
| `summary` | Operator-readable condition |
| `runbook` | Repository documentation anchor |

## Existing entities affected

### AgentHealth / EngineState

- Public readiness shape is minimized and stable.
- Protected operational health may add desired/resident activation fields additively under 022.
- Contract validation continues to ignore additive unknown fields but must validate known field
  types and identity pairing.

### ActiveServingLLM (022)

- Remains the desired platform-wide LLM pointer.
- Is mutated only within an ActivationOperation after validation.
- Must not be treated as evidence of actual residency.

### Prediction

- Continues to store model name/version.
- For LLM inference, those values come from the agent-reported resident identity for the request,
  never from active pointer, environment alias, or incomplete activation state.

## Storage changes

Migration files own the concrete SQL. Conceptually:

```sql
CREATE TABLE schema_migrations (...);

CREATE TABLE activation_operations (...);
CREATE UNIQUE INDEX one_nonterminal_activation
  ON activation_operations ((true))
  WHERE state IN ('prepared', 'committing', 'reloading', 'rolling_back');
```

The exact baseline-stamping and partial-index implementation is validated against Postgres 17 in
the migration contract. The activation table may land with 022 implementation, but its migration
mechanism must exist first.
