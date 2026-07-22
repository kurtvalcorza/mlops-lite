# Feature Specification: Codebase Architecture Hardening — Deepen Modules & Testability Seams

**Feature Branch**: `024-deepen-modules-seams`

**Created**: 2026-07-22

**Status**: Draft

**Input**: User description: "Codebase architecture hardening — deepen modules and testability seams (024). Complete three in-flight/identified extractions (store.py decomposition, promote go-live ordering extraction, agent dispatcher route-table) and record the decisions — including rejected alternatives — as ADRs."

## Context

A read-only architecture review (following the improve-codebase-architecture method) found the platform is already well-architected: deep modules, web-free pure cores split from their I/O wrappers, single-home concepts (`llmresolve`, the `registry.promote` choke-point), explicit fail-open/fail-loud postures, and a maintained `docs/current-architecture.md`. The remaining friction is **finishing patterns the codebase already believes in**, not structural rescue. This feature completes three extractions and records the reasoning (accepted and rejected) so the decisions do not get re-litigated.

## Clarifications

### Session 2026-07-22

- Q: Which findings should the spec cover? → A: All three refactors (store decomposition, promote-ordering extraction, agent route-table) **and** record ADRs for decisions + rejected alternatives.
- Q: How much change is the refactor allowed to make? → A: External gateway/agent API and DB-schema changes are permitted where justified, but must land as a new numbered migration + contract update; behavior-preserving is preferred and any intentional external change is called out explicitly.
- Q: Deliverable shape? → A: Full Spec Kit feature (spec → plan → tasks).
- Q: Definition of done / validation gate? → A: Existing offline suite passes unchanged, every extracted seam gains web-free unit tests, **and** the live ordering tests pass on a brought-up stack.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Decompose the relational + object store (Priority: P1)

`platformlib/store.py` is "two sides, one module": an S3 object store and a relational store with different drivers and failure vocabularies. The activation repository was already lifted into `platformlib/storeimpl/*` behind a re-export facade pinned by `tests/test_store_facade.py`. A maintainer touching one aggregate's storage today must navigate the whole 630-line hotspot; the object-vs-relational boundary is a naming coincidence rather than a module boundary.

**Why this priority**: Highest leverage of the three. The extraction pattern is already proven and low-risk (the facade seam exists and is test-pinned), so each aggregate moves independently with the call sites untouched.

**Independent Test**: After moving one aggregate (e.g. predictions) into its own `storeimpl/` repository, the full offline suite — including `test_store_facade.py` — passes with no change to any `from platformlib import store` call site.

**Acceptance Scenarios**:

1. **Given** the re-export facade is pinned by `test_store_facade.py`, **When** an aggregate's queries move into a per-aggregate `storeimpl/` module, **Then** every `store.<symbol>` call site resolves unchanged and the suite stays green.
2. **Given** the module must load in stdlib-only contexts, **When** `platformlib.store` is imported without boto3 or psycopg installed, **Then** the import succeeds (both drivers stay lazily imported).
3. **Given** the object-store side is split into its own module, **When** the relational store is imported, **Then** no S3/boto3 import path is triggered, and vice-versa.

---

### User Story 2 - Extract the LLM go-live ordering into a testable seam (Priority: P2)

The go-live ordering in `gateway/app/routers/models.py:promote` (version existence → resolve/refuse an unresolvable adapter (FR-265) → assert no activation conflict → gated promote → capture prior pointer → durable activation) is domain sequencing trapped in a FastAPI+httpx module. Because the offline suite has no `fastapi`/`httpx`, this ordering is only reachable through the live HTTP stack. The sequencing carries real invariants (refuse **before** the alias moves; capture the prior pointer **before** it is overwritten) that deserve isolated tests.

**Why this priority**: Concrete testability + locality win, but smaller than US1 — the heavy pieces (the gated alias move, the durable activation) are already deep, tested modules. The value is moving the *ordering* onto the web-free side of the dependency line so it tests like the rest of the domain cores.

**Independent Test**: The extracted ordering module is imported and exercised in the offline suite with fake `registry`/`activation` collaborators (house `test_activation.py`-style fakes), asserting each outcome and that a refusal returns before `registry.promote` is ever called — with no `fastapi`/`httpx` installed.

**Acceptance Scenarios**:

1. **Given** an unresolvable text-generation adapter, **When** go-live runs, **Then** it returns a "refused" outcome and `registry.promote` is never invoked (the alias never moves — FR-265).
2. **Given** a conflicting in-flight activation, **When** go-live runs, **Then** it returns a "conflict" outcome before the alias moves.
3. **Given** a clean text-generation candidate, **When** go-live succeeds, **Then** the prior serving pointer is captured before the pointer is overwritten and the durable activation is invoked.
4. **Given** the extraction, **When** the scheduler or one-click policy path promotes, **Then** it still calls the gated choke-point directly and cannot live-switch the served LLM (FR-275/307/313) — the go-live use-case has exactly one caller (the operator route).
5. **Given** the router refactor, **When** any promote outcome occurs, **Then** the HTTP status codes and `REGISTRY_OPS` metric labels are byte-identical to today's behavior.

---

### User Story 3 - Turn the agent dispatcher into a route table (Priority: P3)

`hostagent/main.py`'s `handle_get`/`handle_post` are ~220 lines of hand-rolled `if path == …` ladders that interleave string path-matching with handler bodies. Each new route grows one long function, and handlers can only be tested by driving path parsing.

**Why this priority**: Lowest leverage — it works and is heavily documented, and the surface is security-sensitive. Worth doing when it next needs real change; sequenced last so it never blocks US1/US2.

**Independent Test**: Each handler is invoked directly (given its dependencies) without constructing the HTTP server or parsing a raw request path, and the agent process still imports with zero pip dependencies.

**Acceptance Scenarios**:

1. **Given** the route table, **When** the agent serves any request, **Then** the public surface is byte-preserved: the open probes stay open, `control/*` stays secret-gated, and the byte-compatible legacy paths still resolve.
2. **Given** the agent's stdlib-only constraint, **When** the agent is imported/started, **Then** no third-party package is required.
3. **Given** a handler, **When** it is unit-tested, **Then** it can be called with fake collaborators without path-string parsing.

---

### User Story 4 - Record the decisions and rejected alternatives as ADRs (Priority: P2)

The review produced decisions worth preserving — most importantly a *rejected* one: unifying the go-live paths was rejected because it would put the LLM live-switch one wiring mistake away from the policy path, endangering FR-275. Without a record, a future contributor may "helpfully" re-suggest it.

**Why this priority**: Cheap, and it protects the invariants US2/US3 rely on. Ships alongside the code it documents.

**Independent Test**: An ADR exists for each accepted decision and each rejected alternative, each stating context, decision, and consequences; a reader can find why the go-live paths are deliberately not shared without reading code.

**Acceptance Scenarios**:

1. **Given** the go-live extraction, **When** a reader consults the ADRs, **Then** they find the recorded rejection of merging the go-live paths (with FR-275 rationale).
2. **Given** the agent route-table work, **When** a reader consults the ADRs, **Then** they find the recorded decision to keep the agent framework-free.
3. **Given** the question "why isn't the serving-LLM selection an MLflow alias?", **When** a reader consults the ADRs, **Then** they find the recorded decision (Postgres pointer, because aliases are per-registered-model and the selection is cross-model) and the rejected "collapse into an alias" alternative — and the note that the pointer wrappers should move out of the MLflow adapter (US1/US2).

---

### Edge Cases

- A store aggregate is referenced by a call site the facade did not re-export → caught by `test_store_facade.py` before merge; the facade surface must be extended, not the call site.
- An external contract or schema change is genuinely needed by a candidate → it must be an explicit, called-out change landing as a new numbered migration + contract update, never an in-place edit to an applied migration or ad-hoc DDL.
- A refactor tempts a behavior change in the gate/shadow/activation decision logic → out of scope (non-goal); such a change belongs to a separate feature.
- The agent route-table refactor risks changing a legacy path's bytes → rejected; byte-compatibility is an acceptance gate.

## Requirements *(mandatory)*

### Functional Requirements

**Store decomposition (US1)**

- **FR-001**: The relational store MUST be decomposed so each aggregate (predictions, labels, capture index, jobs, policies, suggestions) owns a repository module under `platformlib/storeimpl/`, with `platformlib/store.py` reduced to a thin facade.
- **FR-002**: The S3 object-store access MUST be separated from the relational store into its own module.
- **FR-003**: Every existing `from platformlib import store` call site MUST keep working unchanged; the re-export facade surface pinned by `tests/test_store_facade.py` MUST NOT regress.
- **FR-004**: Both drivers (boto3, psycopg) MUST remain lazily imported so the module stays importable in stdlib-only contexts.

**Go-live ordering extraction (US2)**

- **FR-005**: The LLM go-live ordering MUST be extracted into a web-free module (no `fastapi`/`httpx` import) exposing a callable that returns an explicit outcome result object.
- **FR-006**: The ordering invariants MUST be preserved — refuse an unresolvable target before the alias moves (FR-265); capture the prior serving pointer before it is overwritten.
- **FR-007**: The operator promote route MUST become a thin adapter that maps the outcome to HTTP status + the existing `REGISTRY_OPS` metric labels, with no change to the response contract.
- **FR-008**: The go-live use-case MUST remain reachable ONLY from the operator promote route; the scheduler and one-click policy paths MUST continue to call the gated promote choke-point directly and remain unable to live-switch the served LLM (FR-275/307/313).
- **FR-009**: The extracted ordering MUST have web-free unit tests covering each outcome and the refuse-before-alias-moves ordering.

**Agent dispatcher route-table (US3)**

- **FR-010**: The agent's GET/POST dispatch MUST be refactored into an ordered route table separating path matching from handler bodies, with each handler independently testable.
- **FR-011**: The agent MUST remain pip-dependency-free (stdlib-only transport).
- **FR-012**: The public agent surface MUST be byte-preserved — the open probes stay open, `control/*` stays secret-gated, and the byte-compatible legacy paths still resolve.

**Decision records (US4)**

- **FR-013**: Each accepted decision AND each rejected alternative MUST be recorded as an ADR, including the rejection of merging the go-live paths (endangers FR-275), the decision to keep the agent framework-free, and the decision that the platform serving-LLM selection is a Postgres pointer rather than an MLflow `@serving` alias (with "collapse it into an alias" recorded as the rejected alternative).

**Cross-cutting constraints (all stories)**

- **FR-014**: No new heavy dependency may be added to the gateway or agent images (no pandas/scipy/sklearn/Evidently).
- **FR-015**: The fail-open posture on prediction/label/capture WRITES and the fail-loud posture on window/policy/job READS MUST be preserved.
- **FR-016**: Any external gateway/agent API or DB-schema change MUST land as a NEW numbered `platformlib/migrations/*.sql` file plus a contract update; applied migrations MUST NOT be edited and DDL MUST NOT be inlined in code.
- **FR-017**: `docs/current-architecture.md` MUST be updated in the same increment if any Snapshot row changes.

### Key Components *(architectural seams, not data entities)*

- **Relational store facade** (`platformlib/store.py`): the thin re-export surface; owns no aggregate SQL after US1.
- **Aggregate repositories** (`platformlib/storeimpl/*`): one module per aggregate; each owns its schema-seam helpers and queries.
- **Object store module**: the S3 client + paginated listing access, separated from the relational side.
- **Go-live use-case** (new, web-free): the promote ordering + outcome classification; single caller = the operator route.
- **Agent route table** (`hostagent/main.py`): the matching seam; handlers become independently callable.
- **ADR set** (`docs/adr/`): the accepted/rejected decisions.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After each candidate lands, the existing offline test suite passes unchanged — no test is deleted or weakened to make the refactor pass.
- **SC-002**: The offline suite runs to green with neither `fastapi` nor `httpx` installed, and every newly extracted seam has at least one web-free unit test.
- **SC-003**: The live ordering test(s) (e.g. `tests/test_promote_ordering.py`) pass on a brought-up stack (`make up`).
- **SC-004**: `platformlib/store.py` retains no aggregate-specific SQL inline; each aggregate's queries live in its own repository module and the facade is import-only re-exports.
- **SC-005**: The operator promote route handler contains no domain sequencing beyond outcome→HTTP/metric mapping (the ordering lives in the web-free use-case).
- **SC-006**: The number of gated promotion choke-points remains exactly one; no new ungated go-live path exists, and only the operator route reaches the live-switch.
- **SC-007**: Each agent handler is callable in a unit test without constructing the HTTP server or parsing a raw request path.
- **SC-008**: An ADR exists for every accepted decision and every rejected alternative named in FR-013.

## Assumptions

- The three refactor candidates ship as **independent PRs** (each is independently testable and deployable), sequenced P1 → P2 → P3; US4 ADRs ship with the code they document.
- ADRs live under `docs/adr/` (created if absent) as the reasonable default location.
- "External API may change" is *permitted but rarely exercised*: each candidate prefers behavior preservation, and any intentional external-contract change is called out explicitly and gated by FR-016.
- The git working branch is the existing `claude/codebase-architecture-improvements-udog5o`; the spec directory (`specs/024-deepen-modules-seams`, recorded in `.specify/feature.json`) is the source of truth for downstream `/speckit-plan` and `/speckit-tasks`, independent of the branch name.
- No behavioral change to the gate/shadow/activation decision logic, no new modalities/serving engines, and no retired-port/daemon resurrection (explicit non-goals).
