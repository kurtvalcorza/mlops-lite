# Contracts — Surfaces That MUST NOT Regress

This feature is behavior-preserving. The "contracts" here are the existing surfaces each candidate must keep
byte-stable. Any intentional deviation is an explicit, called-out change gated by FR-344 (a new numbered
migration for a schema change; a contract update for an API change — independently) and must be re-checked
against the Constitution Check in plan.md.

## C1 — Store facade surface (US1)

- Every symbol currently importable as `store.<name>` (e.g. `store.StoreError`, `store.LabelExists`,
  `store.s3_client`, `store.create_activation`, the predictions/labels/capture/jobs/policies/suggestions
  operations) MUST remain importable at the same path after decomposition.
- The shared relational plumbing MUST also survive at the same path: `store.dsn`, `store.connect`,
  `store.bootstrap`, `store.ensure_schema`, `store.SCHEMA_VERSION`, `store.TABLES` (relocated to a
  `storeimpl` engine module and re-exported — migration startup and every `store.connect`/`store.bootstrap`
  caller depend on it).
- `platformlib.store` MUST import successfully with neither boto3 nor psycopg installed (lazy drivers).
- The predictions⋈labels window read MUST remain one indexed join (no return to O(N) object scans).
- **Guard**: `tests/test_store_facade.py` (existing) + `tests/test_store_decomposition.py` (new, per-aggregate).

## C2 — Promote request/response + observability (US2)

- Request body unchanged: `{version, override?, preempt?}`.
- Response shape unchanged: the 022/023 `{promoted, verdict, serving_llm?, activation?}` payload, including the
  `serving_llm` object on a text-generation promote and the `rolled_back` field on an unresolvable reload.
- Status-code mapping unchanged (see data-model.md §Router mapping): refuse/conflict → 409; gate-block →
  200 with `promoted:false`; success → 200 with `promoted:true`; **registry pre-check / promote-time (alias)
  errors that escape the handler → 502**.
- **Activation-store failures are NOT 502** (preserve exactly): `ActivationService.activate`
  (`gateway/app/activation.py`) deliberately absorbs a down/unmigrated activation store into a **200**
  response — the `_untracked` single-shot fallback, a failed pointer write surfaced as `{active:null, error}`
  in the 200 body, and a mid-flight store loss returned as a visible interrupted activation
  (`activation.state:"unknown"`, reconciler resumes). The extraction MUST keep these as 200 outcomes; only the
  pre-check/promote exceptions that currently escape map to 502. Do NOT re-route the broad "store error"
  category to 502.
- `REGISTRY_OPS` counter labels unchanged: `op="promote"` with `status ∈ {refused, conflict, blocked, ok,
  unresolvable, error}` — emitted at the same decision points.
- **Invariant**: exactly one gated promotion choke-point (`registry.promote`) and exactly one live-switch
  caller (the operator route via `promotion.go_live`). The scheduler/policy paths remain unable to
  live-switch (FR-275/307/313).
- **Guard**: `tests/test_promotion_ordering.py` (new, web-free) + `tests/test_promote_ordering.py` (existing,
  live leg) + the existing `test_promotion_gate.py` / `test_promotion_modes.py` suites unchanged.

## C3 — Agent public surface (US3)

- Open, unauthenticated GET probes exactly: `/healthz`, `/readyz`, `/metrics` (byte-identical payload shapes).
- Keyed operational `/health` and per-engine `/engines/<id>/{health,readyz,healthz}` unchanged.
- `POST /control/unload` and `POST /control/reload` remain secret-gated (`X-Agent-Control`).
- Byte-compatible legacy job/train aliases still resolve to the same handlers.
- Agent process imports and starts with **zero** third-party packages (stdlib-only).
- **Guard**: `tests/test_agent_routes.py` (new, handler-level) + existing `test_agent_http.py`,
  `test_agent_jobs_http.py`, `test_agent_auth.py` unchanged.

## C4 — Cross-cutting

- No new heavy dependency in the gateway or agent images (FR-342).
- Fail-open (drop-counter) on background prediction/capture WRITES; **fail-loud** (`QualityStoreError`→502) on operator-facing label attach AND on window/policy/job READS (FR-343).
- `docs/current-architecture.md` updated in the same increment if any Snapshot row changes (FR-345) — none
  expected, since topology/authority/trust-boundaries are unaffected by internal module moves.
