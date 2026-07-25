# ADR 0001 — Decompose `platformlib/store.py` into per-aggregate repositories behind a facade

**Status**: Accepted (feature 024 US1)

## Context

`platformlib.store` is the storage entry point ~28 call sites import (`from platformlib import store`;
`store.log_prediction(...)`). It had grown into a **630-LOC, two-sides hotspot**: an S3 object-store side
(a cached client + paginated listings) and a relational side spanning seven aggregates (predictions,
labels, capture index, jobs, policies, suggestions, the serving-LLM pointer) plus shared connection /
migration plumbing. A maintainer touching one aggregate had to navigate the whole file; the object-vs-
relational boundary was a naming coincidence, not a module boundary. The activation repository had already
been lifted into `storeimpl/activations.py` behind a re-export facade pinned by `tests/test_store_facade.py`
— a proven, in-production seam.

## Decision

Repeat that proven seam for the rest:

- One repository module per relational aggregate under `platformlib/storeimpl/` (`predictions.py`,
  `labels.py`, `capture.py`, `jobs.py`, `policies.py`, `suggestions.py`, `serving_llm.py`), each stateless
  (takes a live `conn`) and importing only the shared error/seam helpers from `storeimpl/_base.py`.
- The shared relational plumbing (`dsn`/`connect`/`bootstrap`/`ensure_schema`/`SCHEMA_VERSION`/`TABLES`)
  gets an explicit home in `storeimpl/_engine.py` — it is not aggregate SQL, but it is not re-exportable
  from nowhere.
- The object-store access (`s3_client()` + `list_keys`/`list_common_prefixes`) is **consolidated into the
  existing `platformlib/s3io.py`** — the shared Garage authority already used by batch/quality/validation —
  NOT a new `objectstore.py`, which would be a second S3 home.
- `platformlib/store.py` becomes a **thin re-export facade** holding no aggregate SQL, so every
  `store.<name>` call site resolves unchanged. `tests/test_store_facade.py` pins the surface.

Both drivers stay LAZY — boto3 inside the s3io factory, psycopg inside `connect()` / the write primitives —
so importing `store` never requires a driver (the native daemons and offline env load it driver-free).

## Consequences

- **Each aggregate is independently readable and testable**; the hotspot is gone. Behavior is preserved by
  **test parity** (ADR 0004): the full offline suite passes unchanged with zero call-site edits.
- **Two accepted micro-costs.** (1) `_s3()` (fresh per call) and `s3_client()` (process-cached) stay TWO
  functions in one home — merging them into one factory is a behavior change (`_s3`'s per-call build is
  load-bearing for the missing-creds-raises contract), so behavior-preservation keeps them distinct.
  (2) A few private helpers a test fake reaches through the facade (`store._job_split`,
  `store._job_row_to_record`) are re-exported explicitly.
- **Rejected alternatives.** *Leave it as one module* — the readability/testability cost was the whole
  reason. *Introduce an ORM* — pulls a heavy dependency (Principle III) for SQL the aggregates already
  express in a few lines, and hides the exact query shapes the monitoring windows depend on. *A new
  `objectstore.py`* — a second S3 home alongside `s3io.py` (rejected by review; see the s3io consolidation).

## References

- `specs/024-deepen-modules-seams/` US1 (T562–T572); `contracts/preservation.md` §C1.
- `platformlib/storeimpl/*`, `platformlib/s3io.py`, `platformlib/store.py`.
- `tests/test_store_facade.py` (surface pin) + `tests/test_store_decomposition.py` (homes + lazy drivers).
- ADR 0004 (the test-parity gate this refactor was proven by).
