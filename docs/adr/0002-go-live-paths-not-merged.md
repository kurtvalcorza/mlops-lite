# ADR 0002 — Do NOT merge the three `registry.promote` callers into one go-live path

**Status**: Rejected alternative (recorded under feature 024 US2)

## Context

Three code paths call `registry.promote` (the single gated alias-move choke-point):

1. the **operator promote route** (`gateway/app/routers/models.py:promote`) — the ONE go-live surface,
   which for a text-generation version also live-switches the served LLM (moves the alias, writes the
   `serving_llm` pointer, drives the durable agent reload);
2. the **scheduler's auto-on-green path** (`gateway/app/scheduler.py:_default_promote`);
3. **suggestion acceptance** (`gateway/app/routers/policies.py`).

While extracting the operator route's ordering into a web-free `promotion.go_live` use-case (024 US2), an
obvious-looking simplification presented itself: route ALL three callers through one unified `go_live` so
there is a single promote implementation.

## Decision

**Rejected.** The three paths are deliberately different, and the difference is a NON-NEGOTIABLE invariant.
Only the operator route may **live-switch** the served LLM (pointer write + agent reload); the scheduler
and suggestion paths may **gate a candidate** (move the alias) but MUST NOT live-switch text generation
(FR-275 / FR-307 / FR-313). `promotion.go_live` therefore has **exactly one caller** — the operator route —
and the automatic paths keep calling `registry.promote` directly.

## Consequences

- **The single-live-switch invariant is structural, not conventional.** `tests/test_promotion_ordering.py`
  greps the codebase and asserts `go_live` has exactly one caller (SC-170); a future refactor that wires a
  second caller fails the test loudly.
- **Merging would have re-created the exact hazard 022/023 designed out**: an automatic policy tick could
  live-switch the operator's served LLM out from under them (a background action changing production
  serving with no operator in the loop).
- **The shared logic that IS safe to share stays shared** — the gate + alias move are `registry.promote`,
  which all three already call. Only the *live-switch half* (pointer + reload ordering) is operator-only,
  and that is what `go_live` owns.
- If someone later proposes "just have the scheduler call `go_live` too," this ADR is the answer: it would
  break FR-275/307/313. A genuinely-wanted automatic live-switch would need its own explicit spec + operator
  confirmation model, not a quiet unification.

## References

- `specs/024-deepen-modules-seams/` US2 (T573–T576); `contracts/preservation.md` §C2.
- `gateway/app/promotion.py` (`go_live`), `gateway/app/routers/models.py:promote`,
  `gateway/app/scheduler.py:_default_promote`, `gateway/app/routers/policies.py`.
- FR-275 / FR-307 / FR-313 (single live-switch), SC-170 (single caller).
