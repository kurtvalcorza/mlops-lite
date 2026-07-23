# Phase 1 Data Model — Seams & Interfaces

This is a refactor: there is **no new persisted data model** and no schema change (FR-344 would gate one if
it appeared). The "model" here is the set of code seams the extractions introduce.

## Relational store seams (US1)

Each aggregate repository under `platformlib/storeimpl/` owns its table's access and is stateless (takes a
live `conn`). Shared error/seam helpers stay in `storeimpl/_base.py`.

| Repository module | Owns (table[s]) | Representative operations | Failure posture |
|---|---|---|---|
| `predictions.py` | `predictions` | insert prediction row; window join with labels | WRITE fail-open (drop-counter); READ fail-loud |
| `labels.py` | `labels` | write-once insert (PK-enforced) → `LabelExists` | WRITE fail-open; duplicate ⇒ `LabelExists` |
| `capture.py` | `capture_index` | insert capture row; list by window | WRITE fail-open; READ fail-loud |
| `jobs.py` | jobs state | upsert/get job status | READ fail-loud |
| `policies.py` | policy rows | CRUD policy + status | READ fail-loud |
| `suggestions.py` | promotion suggestions | create/resolve/get suggestion | READ fail-loud |
| `activations.py` *(exists)* | `activation_operations` | CAS activation lifecycle | as today |

**Facade invariant**: every symbol currently reachable as `store.<name>` remains reachable after the move
(re-exported in `platformlib/store.py`). `tests/test_store_facade.py` is the pinned guard.

**Object store**: `platformlib/objectstore.py` owns `s3_client()` + paginated listings; imports boto3 lazily.
No relational import path is triggered by importing it, and vice-versa.

## Go-live seam (US2)

`gateway/app/promotion.py` (web-free) exposes one entry point returning an explicit outcome.

```
GoLiveOutcome (enum): REFUSED | CONFLICT | BLOCKED | PROMOTED | ERROR

GoLiveResult:
  outcome: GoLiveOutcome
  body: dict                 # the response payload the router returns as-is
  metric_status: str         # the REGISTRY_OPS label ("refused"|"conflict"|"blocked"|"ok"|"unresolvable"|"error")

go_live(name, version, *, override, preempt, registry, activation) -> GoLiveResult
```

**Ordering invariants encoded in the seam**:
1. An unresolvable target ⇒ `REFUSED` **before** `registry.promote` is called (alias never moves — FR-265).
2. A conflicting in-flight activation ⇒ `CONFLICT` **before** the alias moves.
3. On success, the prior serving pointer is captured **before** it is overwritten, then the durable
   activation runs.

**Router mapping** (`routers/models.py:promote`, thin adapter — byte-identical to today):

| outcome | HTTP status | REGISTRY_OPS status label |
|---|---|---|
| REFUSED | 409 | `refused` |
| CONFLICT | 409 | `conflict` |
| BLOCKED | 200 (`promoted:false`) | `blocked` |
| PROMOTED | 200 (`promoted:true`) | `ok` (+ `unresolvable` on rollback) |
| ERROR | 502 | `error` |

**Single-caller invariant**: `go_live()` is called **only** by the operator promote route. The scheduler
and one-click policy paths keep calling `registry.promote` directly and cannot reach `go_live()`
(FR-336 / SC-170).

## Agent route-table seam (US3)

`hostagent/main.py` dispatch becomes an ordered table; handlers are pure-ish functions of their
dependencies, callable without the HTTP server.

```
Route:
  method: "GET" | "POST"
  match(path) -> bool                # exact, prefix, or suffix predicate (parsed path, not raw)
  handler(ctx) -> response           # ctx carries admission/journal/manager/jobs/policy + parsed body

# First matching route wins; unmatched ⇒ 404. Public surface preserved:
#   open GET probes (/healthz /readyz /metrics), keyed /health, secret-gated /control/*,
#   byte-compatible legacy job/train aliases.
```

No route's response bytes change; the table only relocates the matching decision out of the handler bodies.
