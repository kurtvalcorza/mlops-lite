# Quickstart — Validating the Architecture Hardening

This feature is proven by **test parity**: nothing behavioral changes, so the gate is "the same tests still
pass, plus new web-free tests for each extracted seam, plus the live ordering leg." No implementation code
lives here — see tasks.md (after `/speckit-tasks`) for the work items.

## Prerequisites

- Repo checked out on the working branch; Python env for the offline suite (stdlib + pytest).
- **Offline suite has no `fastapi`/`httpx`** — that is intentional and is the property US2 relies on.
- For the live leg only: the stack up (`make up`) with a gateway API key.

## 1. Offline suite stays green (SC-001, SC-002)

```bash
make test            # or: pytest -q
```

Expected: all existing tests pass unchanged, and the suite runs to green **without** fastapi/httpx installed.
No existing test is deleted or weakened.

## 2. Store decomposition seam (US1 → C1)

```bash
pytest -q tests/test_store_facade.py tests/test_store_decomposition.py
python -c "import platformlib.store; import platformlib.objectstore; print('lazy import OK')"
```

Expected: the facade surface is intact (every `store.<name>` resolves), each aggregate repository has direct
coverage, and both modules import with neither boto3 nor psycopg installed.

## 3. Go-live ordering seam (US2 → C2)

```bash
pytest -q tests/test_promotion_ordering.py
```

Expected (web-free, fake registry/activation collaborators):
- unresolvable adapter ⇒ `REFUSED` and `registry.promote` was **never** called;
- conflicting activation ⇒ `CONFLICT` before the alias moves;
- clean candidate ⇒ prior pointer captured before overwrite, then durable activation invoked;
- outcome→HTTP/metric mapping matches the table in `contracts/preservation.md` §C2;
- `promotion.go_live()` has exactly one caller (the operator route) — scheduler/policy paths cannot reach it.

## 4. Agent route-table seam (US3 → C3)

```bash
pytest -q tests/test_agent_routes.py tests/test_agent_http.py tests/test_agent_auth.py
```

Expected: each handler is callable without constructing the HTTP server; the public surface (three open GET
probes, keyed `/health`, secret-gated `/control/*`, legacy aliases) is byte-preserved; agent imports with no
third-party package.

## 5. Live ordering leg (SC-003)

```bash
make up
pytest -q tests/test_promote_ordering.py     # skips cleanly if the stack is down
```

Expected: on a brought-up stack, an unresolvable text-generation adapter is refused (409) and the `@serving`
pointer is unchanged before/after — the ordering invariant holds end-to-end.

## 6. Decisions recorded (US4 → SC-008)

```bash
ls docs/adr/
```

Expected: an ADR per accepted decision and per rejected alternative — including the rejection of merging the
go-live paths (FR-275 rationale) and keeping the agent framework-free.

## Done signal

All of the above green, `docs/current-architecture.md` reviewed for Snapshot drift (none expected), and the
three candidates landed as independent, individually-revertable PRs (P1 → P2 → P3).
