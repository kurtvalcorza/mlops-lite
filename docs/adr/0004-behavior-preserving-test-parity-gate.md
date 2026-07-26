# ADR 0004 — Refactors are gated by test parity, not by reasoning alone

**Status**: Accepted (feature 024)

## Context

Feature 024 is explicitly **behavior-preserving**: it decomposes the store (US1), extracts the go-live
ordering (US2), and route-tables the agent (US3) WITHOUT changing what the platform does. The risk in any
"pure refactor" is a subtle behavior change that a human reviewer — and the author — reason past. The
codebase already had the raw material for a stronger gate: a large offline suite, a facade-pinning test, and
web-free seam tests.

## Decision

Every 024 slice is gated by **test parity**, in three layers:

1. **The existing offline suite passes UNCHANGED** — no test is weakened, deleted, or edited to make a
   refactor pass. Zero call-site edits where the contract is "callers unchanged."
2. **Every extracted seam gains a web-free unit test** — the store facade/decomposition pins, the go-live
   ordering over fakes, the agent handlers called without a socket.
3. **The live-only legs run on a brought-up stack** (e.g. `test_promote_ordering.py` on `make up`) for the
   ordering reachable only through the HTTP path.

A refactor is "done" only when all three are green, on top of `ruff` + `scripts/check_specs.py`.

## Consequences

- **This gate caught real behavior changes during 024 that reasoning had waved through:**
  - US1: consolidating `_s3()` into one cached factory broke `test_s3io_client_missing_creds_raises_by_name`
    — its per-call build is load-bearing; the suite forced keeping two functions.
  - US1: a test fake reached `store._job_split` through the facade; the suite failed until it was re-exported.
  - US5: a new test importing `gateway.app.monitoring` (vs the canonical `app.monitoring`) re-registered a
    prometheus Gauge and collided; the suite failed until the import matched the suite convention.
- **Cost:** the gate needs a runnable offline suite (`requirements-dev.txt`), and the live legs need hardware
  — so the `[HW]` legs are validated on the box, not offline (flagged, never skipped).
- **Rejected alternative — "review + reasoning is enough" for a pure refactor.** Each of the three catches
  above would have shipped a real regression under review-only. The parity gate is cheap insurance that the
  "behavior-preserving" claim is a fact, not an intention.

## References

- `specs/024-deepen-modules-seams/` (SC-165 test-parity; the per-US independent tests); `quickstart.md`.
- `tests/test_store_facade.py`, `tests/test_store_decomposition.py`, `tests/test_promotion_ordering.py`,
  `tests/test_agent_routes.py`, `tests/test_promote_ordering.py` (live leg).
- ADR 0001 / 0002 / 0003 (the refactors this gate proved).
