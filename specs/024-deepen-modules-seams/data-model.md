# Phase 1 Data Model — Seams & Interfaces

This is a refactor: there is **no new persisted data model** and no schema change (FR-344 would gate one if
it appeared). The "model" here is the set of code seams the extractions introduce.

## Relational store seams (US1)

Each aggregate repository under `platformlib/storeimpl/` owns its table's access and is stateless (takes a
live `conn`). Shared error/seam helpers stay in `storeimpl/_base.py`.

| Repository module | Owns (table[s]) | Representative operations | Failure posture |
|---|---|---|---|
| `predictions.py` | `predictions` | insert prediction row; window join with labels | WRITE **propagates** (raises) — fail-open/drop-counter is the `quality` wrapper's job, NOT the repo's; READ fail-loud |
| `labels.py` | `labels` | write-once insert (PK-enforced) → `LabelExists` | **WRITE fail-loud** (`QualityStoreError`→502 — operator-facing label attach); duplicate ⇒ `LabelExists` |
| `capture.py` | `capture_index` | insert capture row; list by window | WRITE **propagates** (raises) — fail-open is the `quality` wrapper's job, not the repo's; READ fail-loud |
| `jobs.py` | jobs state | upsert/get job status | READ fail-loud |
| `policies.py` | policy rows | CRUD policy + status | READ fail-loud |
| `suggestions.py` | promotion suggestions | create/resolve/get suggestion | READ fail-loud |
| `activations.py` *(exists)* | `activation_operations` | CAS activation lifecycle | as today |

**Facade invariant**: every symbol currently reachable as `store.<name>` remains reachable after the move
(re-exported in `platformlib/store.py`). `tests/test_store_facade.py` is the pinned guard.

**Object store**: the existing `platformlib/s3io.py` (the shared Garage authority) gains the store's cached
`s3_client()` + the paginated listings, homed alongside its per-call `_s3()`; boto3 imported lazily. (The
two access patterns are NOT merged into one factory: `_s3()` builds fresh per call and that behavior is
load-bearing — the missing-creds-raises contract relies on it — so behavior-preservation keeps them
distinct, one home, two functions.) No relational import path is triggered by importing it, and vice-versa.
No new `objectstore.py` is created — that would be
a second S3 home alongside `s3io.py`.

## Go-live seam (US2)

`gateway/app/promotion.py` (web-free) exposes one entry point returning an explicit outcome.

```
GoLiveOutcome (enum): NOT_FOUND | REFUSED | CONFLICT | BLOCKED | PROMOTED | ERROR

GoLiveResult:
  outcome: GoLiveOutcome
  body: dict                 # the response payload the router returns as-is
  metric_statuses: list[str] # REGISTRY_OPS label(s) to emit, IN ORDER — usually one, but a
                             #   PROMOTED-then-rolled-back activation emits ["ok", "unresolvable"]
                             #   (a singular field would drop one of the two existing increments)
                             #   values: not_found|refused|conflict|blocked|ok|unresolvable|error

go_live(name, version, *, override, preempt, registry, activation) -> GoLiveResult
```

**Ordering invariants encoded in the seam**:
1. An unresolvable target ⇒ `REFUSED` **before** `registry.promote` is called (alias never moves — FR-265).
2. A conflicting in-flight activation caught by the pre-check (`assert_no_conflict`) ⇒ `CONFLICT` **before**
   the alias moves (emits `conflict` only).
3. On success, the prior serving pointer is captured **before** it is overwritten, then the durable
   activation runs.
4. **Post-promote conflict (TOCTOU — preserve exactly):** the pre-check has a documented race — a different
   activation can start between `assert_no_conflict` and `activate()`'s `submit`. When it does,
   `registry.promote` has already moved the alias and emitted `ok`, then `activate()` raises
   `ActivationError`; the route emits a SECOND label `conflict` and returns 409 with the **alias left moved**
   (`models.py:114-145`). The seam MUST represent this as `metric_statuses == ["ok", "conflict"]` (the list
   field already carries multi-emit, exactly like PROMOTED's `["ok","unresolvable"]`) — never collapse it to
   a single pre-alias `conflict`, and do NOT "repair" the moved alias (that would be a behavior change out of
   scope for this behavior-preserving feature).

**Router mapping** (`routers/models.py:promote`, thin adapter — byte-identical to today):

| outcome | HTTP status | REGISTRY_OPS status label |
|---|---|---|
| NOT_FOUND | 404 | (none — preserves the current pre-metric 404 at `routers/models.py:105-106` for a missing version) |
| REFUSED | 409 | `refused` |
| CONFLICT | 409 | `conflict` (pre-alias, via `assert_no_conflict`); the **post-promote TOCTOU** path instead emits `ok` **then** `conflict` with the alias left moved — see invariant 4 |
| BLOCKED | 200 (`promoted:false`) | `blocked` |
| PROMOTED | 200 (`promoted:true`) | `ok`, THEN `unresolvable` on rollback — **two** emits in order (not one) |
| ERROR | 502 | `error` on a **promote-time** failure; **empty (no emit)** on a **pre-check** failure — version-list / `llm_target_info` `RegistryError` return 502 *before* any `REGISTRY_OPS` increment (`models.py:101-110`) |

**Not-found ordering**: version existence is checked first; a missing version ⇒ `NOT_FOUND` ⇒ 404 with **no** `REGISTRY_OPS` emit (matching today's route, which raises the 404 before any metric increment). The web-free use-case returns this outcome; it never raises an HTTP-specific exception.

**Activation-store failures stay in the 200 body** (not 502): on the PROMOTED path the gate has passed and the alias has moved, so `ActivationService.activate` runs and a down/unmigrated *activation* store is absorbed into the 200 response's `serving_llm`/`activation` fields — the `_untracked` single-shot fallback, a failed pointer write as `{active:null, error}`, or a mid-flight loss as `activation.state:"unknown"` (reconciler resumes). Only the *registry* pre-check/promote exceptions in the ERROR row become 502. The extraction MUST NOT widen 502 to the activation store (see contracts/preservation.md §C2).

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
