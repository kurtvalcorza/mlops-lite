# ADR 0003 — The host agent dispatches with a stdlib route table, not a web framework

**Status**: Accepted (feature 024 US3)

## Context

The native host agent (`hostagent/main.py`) is **stdlib-only by constitution** (Principle III,
FR-339): it runs on the WSL box beside the GPU with zero pip dependencies, on `http.server`. Its request
dispatch had grown into two hand-rolled `if path == …` ladders (`handle_get`/`handle_post`) — hard to read,
and the handler bodies could only be exercised by standing up the HTTP server and issuing real requests.

The tempting fix for "clean routing + testable handlers" is to adopt a micro web framework (Flask/FastAPI/
Starlette) with decorator routes and dependency injection.

## Decision

**Keep the agent framework-free.** Turn the if-ladders into ordered `(matcher, handler)` **route tables**
(`_GET_ROUTES` / `_POST_ROUTES`) built from plain stdlib: each former branch body becomes a named
`(path, ctx)` handler, `handle_get`/`handle_post` iterate the table (first match wins → legacy fallback →
404), and `ctx` is a `types.SimpleNamespace` of the deps the dispatcher already received. No new import
beyond the standard library.

## Consequences

- **The public surface is byte-preserved** (FR-340): parsed-path routing, open probes (`/healthz`/`/readyz`/
  `/metrics`), the keyed `/health`, secret-gated `/control/*`, and the byte-compatible legacy trainer aliases
  all route exactly as before. The existing `test_agent_http`/`jobs_http`/`auth`/`engines` suites pass
  unchanged.
- **Handlers are unit-callable without a socket** — `tests/test_agent_routes.py` drives them with fake
  admission/journal/manager/jobs, and asserts the public route set (contracts/preservation.md §C3).
- **The stdlib-only guarantee holds** (FR-339): the agent imports and starts with every third-party package
  blocked (verified in the suite). A framework would have added a pip dependency to the one process that must
  stay dependency-light on the GPU box.
- **Rejected alternative — adopt a web framework.** It buys routing/DI the route table already provides, at
  the cost of the constitution's stdlib-only agent (and a heavier cold-start / attack surface on the box).
  If the agent's surface ever grows past what a stdlib table cleanly expresses, revisit — but that is a
  constitution-level change, not a convenience.

## References

- `specs/024-deepen-modules-seams/` US3 (T584–T587); `contracts/preservation.md` §C3.
- `hostagent/main.py` (`_GET_ROUTES`/`_POST_ROUTES`, the `_get_*`/`_post_*` handlers), `tests/test_agent_routes.py`.
- Principle III (lightweight), FR-339 (stdlib-only agent), FR-340 (byte-preserved surface).
