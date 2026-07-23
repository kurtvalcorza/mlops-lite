# Phase 0 Research — Deepen Modules & Testability Seams

All spec-level decisions were resolved in the `/speckit-specify` clarifications (see spec.md §Clarifications).
This file records the *how* for each extraction and, critically, the rejected alternatives so they are not
re-litigated.

## D1 — Store decomposition: continue the proven facade-preserving move

**Decision**: Extract one repository module per relational aggregate (predictions, labels, capture, jobs,
policies, suggestions) under `platformlib/storeimpl/`, and split the S3 object-store access into
`platformlib/objectstore.py`. `platformlib/store.py` becomes a thin re-export facade; the ~28
`from platformlib import store` call sites are untouched.

**Rationale**: The pattern is already in production — the activation repository was lifted into
`storeimpl/activations.py` behind re-exports pinned by `tests/test_store_facade.py`. Repeating a proven,
test-guarded move per aggregate makes each step independently reversible and keeps the blast radius at
"module-internal." Both drivers stay lazily imported (boto3 in the object-store module, psycopg in the
repositories) so the stdlib-importability the native daemons rely on is preserved.

**Alternatives considered**:
- *Leave store.py as-is* — rejected: the "two sides, one module" naming coincidence and 630-LOC hotspot
  are the exact friction the review flagged; the seam already exists, so stopping halfway is the worse state.
- *One big `relational.py` split only* — rejected: aggregates have independent lifecycles and failure
  vocabularies; per-aggregate modules give locality that a single relational blob does not.
- *Change the facade surface to something cleaner* — rejected for this feature: the call sites and
  `test_store_facade.py` pin the surface; changing it is a separate, behavior-visible decision.

## D2 — Go-live ordering: extract a web-free use-case, keep it single-caller

**Decision**: Move the promote ordering (existence → resolve/refuse unresolvable adapter → assert no
conflict → gated promote → capture prior pointer → durable activation) into `gateway/app/promotion.py`,
a module that imports **no** fastapi/httpx (like `activation.py`/`evaluation.py`). It returns a
`GoLiveOutcome` result object; `routers/models.py:promote` maps the outcome to HTTP status + `REGISTRY_OPS`
labels with no response-contract change.

**Rationale**: The offline suite has no fastapi/httpx, so the ordering — which carries real invariants
(refuse *before* the alias moves, FR-265; capture prior *before* overwrite) — is currently only reachable
through the live HTTP stack. Putting it on the web-free side of the dependency line makes it unit-test like
the other domain cores, with `tests/_activation.py`-style fakes.

**Rejected alternative (the important one)**: **Do NOT merge the go-live paths.** An earlier framing
proposed a single shared promotion use-case for all three callers (operator route, one-click policy accept,
auto-on-green scheduler). Grilling rejected it: only the operator route may live-switch the served LLM
(FR-275/307/313); the other two deliberately call the gated `registry.promote` directly and must stay
unable to activate. A shared use-case would put the live-switch one wiring mistake away from the policy
path. The extracted `promotion.go_live()` therefore has **exactly one caller** and the invariant is
enforced structurally. This is recorded as ADR-002.

**Alternatives considered**:
- *Test the router handler directly with monkeypatched modules* — rejected: impossible offline (importing
  the router requires fastapi/httpx); it would force those deps into the offline suite, breaking the
  dependency-light CI stance.
- *Leave it router-resident, cover only via live tests* — rejected: the ordering invariants deserve fast,
  isolated coverage; `tests/test_promote_ordering.py` (already on this branch) stays as the live leg (SC-167)
  but is not a substitute for offline ordering tests.

## D3 — Agent dispatcher: stdlib route table, framework stays out

**Decision**: Replace `handle_get`/`handle_post` if-ladders with an ordered route table (matcher → handler)
in `hostagent/main.py`, separating path-matching from handler bodies so each handler is unit-callable.

**Rationale**: ~220 lines interleave string matching with handler logic; each new route grows one function
and handlers can only be tested through path parsing. A table isolates the matching seam.

**Rejected alternative**: **Introduce a web framework (FastAPI/Starlette) in the agent.** Rejected: the
agent's stdlib-only, pip-dep-free transport is a deliberate, validated decision (the dual-runtime drill kept
stdlib; `AGENT_RUNTIME`/`asgi.py` were deleted at 023 US6). The table is hand-rolled stdlib. Recorded as
ADR-003. Lowest priority (P3) and sequenced last so it never blocks US1/US2; the public surface is
byte-preserved (FR-340).

## D4 — ADR placement and format

**Decision**: ADRs live under `docs/adr/` as `NNNN-title.md`, each with Context / Decision / Consequences,
and each rejected alternative gets its own ADR (status: Rejected) so future contributors find *why not*.

**Rationale**: The repo has rich `docs/` but no ADR home; a conventional lightweight ADR format is the
cheapest durable record. The rejected-path ADRs (D2, D3) are the highest-value ones.

**Alternatives considered**: inline comments only (rejected — not discoverable as decisions); a single
decisions.md log (rejected — one-file-per-decision diffs cleaner and links from PRs).

## D5 — Validation gate: behavior preservation proven by test parity

**Decision**: Each candidate lands only when (a) the existing offline suite passes unchanged, (b) the new
seam has a web-free unit test, and (c) for US2, the live `test_promote_ordering.py` passes on `make up`.

**Rationale**: For a refactor, "no behavior change" is the whole contract; test parity is how it is proven.
No test may be weakened to make a refactor pass (SC-165). External-contract/schema changes are permitted
but must be explicit and gated by FR-344 — none are anticipated.
