# Tasks: 018 Review Remediation

**Input**: Design documents from `specs/019-review-remediation-018/` (spec.md, plan.md).

> **Status (2026-07-03):** Draft — remediation of ten verified findings from the high-effort review of the
> 018 fold-in. Not yet implemented. IDs continue the shared space (FR-188..197, SC-117..126, T382..T400).
> Each fix is TDD: write the failing regression test first (it MUST fail on current `HEAD`), then the fix.

## Format: `[ID] [P?] [Story] Description`

- **[P]** = parallelizable (different files, no dependency). `[USx]` maps to the spec's user stories.
- Each user story is an independent slice: implementable, testable, and mergeable on its own.

---

## Phase 1: User Story 1 — durable journal (P1) 🎯

**Goal**: A torn-tail crash never loses a durably-appended transition (FR-188/189). **Test**: torn file →
append → replay loses zero records; a failed append never advances in-memory state.

- [ ] **T382** [P] [US1] `tests/test_agent_journal.py` — add cases: (a) write record A, truncate off its
  trailing newline, `_append` record B, then `_replay` → **both** A and B are recovered as separate lines;
  (b) a `_append` whose `f.write`/fsync raises leaves `get()`/`active_count()` reporting the *pre*-transition
  state (memory does not lead the log). Both MUST fail on current `HEAD`.
- [ ] **T383** [US1] `hostagent/journal.py` `_append`: before writing, seek to EOF and repair a torn tail —
  if the file is non-empty and does not end in `\n`, truncate the partial line (or write a leading `\n`) so
  every record occupies its own parseable line; fsync the containing directory on first file create.
- [ ] **T384** [US1] `hostagent/journal.py` `transition()`/`submit()`: perform the durable append *before*
  mutating the in-memory record (or roll the mutation back if the append raises) so memory never leads the
  durable log; propagate the failure to the caller.

**Checkpoint**: SC-117 green — zero record loss across torn-tail-append-replay.

---

## Phase 2: User Story 2 — GPU lease self-recovery (P1) 🎯

**Goal**: A dropped/failed release never permanently wedges other tenants (FR-190). **Test**: stale
ours-wrong-tenant record → next different-tenant acquire self-heals; a different live PID is still refused.

- [ ] **T385** [P] [US2] `tests/test_lockfile_interop.py` (or `tests/test_state_dir.py`) — write a lockfile
  record owned by the current live PID for tenant `llm-serving`, then `acquire("vision")` → succeeds by
  reclaiming our own record; a record owned by a *different* live PID still raises `LeaseHeld`. First case
  MUST fail on current `HEAD`.
- [ ] **T386** [US2] `serving/gpu_lease.py` `acquire()`: in the ours-but-different-tenant branch, reclaim
  (self-heal) the same-owner record instead of `raise LeaseHeld`; leave the different-owner refusal intact.
- [ ] **T387** [P] [US2] `hostagent/admission.py` `release()`: do not silently swallow a failed
  `lease.release()` — log it (and/or reconcile on the next acquire) so a skipped release is observable.

**Checkpoint**: SC-118 green — no restart-only wedge; cross-process exclusion preserved.

---

## Phase 3: User Story 3 — swap never evicts for an unready target (P1) 🎯

**Goal**: A non-2xx target refuses the swap before eviction (FR-191). **Test**: 503 target → resident model
untouched; 200 target still swaps.

- [ ] **T388** [P] [US3] `tests/test_swap_orchestration.py` — stub the target probe endpoint to return 503;
  assert `preempt_if_needed` refuses (no `unload_holder` call, resident holder still resident); a 200 target
  still swaps. 503 case MUST fail on current `HEAD`.
- [ ] **T389** [US3] `gateway/app/swap.py` `_default_target_probe`: return reachable only on a serve-ready
  status (2xx / explicit readiness), not on any HTTP response; a non-2xx refuses the swap and the refusal is
  distinguishable from "holder busy / not preemptable".

**Checkpoint**: SC-119 green — no evict-then-fail outage.

---

## Phase 4: User Story 4 — idempotent retrain launch (P1) 🎯

**Goal**: Exactly one training run per breach under partial failure (FR-192). **Test**: lost launch response
and double store-write failure each launch exactly one run; a genuinely-failed launch still retries.

- [ ] **T390** [P] [US4] `tests/test_policy_scheduler.py` — (a) `/train` POST raises *after* the run is
  accepted → next due check does **not** dispatch a second run; (b) both `clear_pending` and the `landed`
  `save_pending` fail → next tick does **not** re-launch; (c) a launch that never created a run **is**
  retried. (a)/(b) MUST fail on current `HEAD`.
- [ ] **T391** [US4] `gateway/app/scheduler.py`: attach a stable per-breach run/idempotency key to
  `_default_launch`'s `/train` body, and mark the park **launched before** issuing the request so a lost
  response is reconciled as launched (not failed); a launch that provably never started stays retriable.
- [ ] **T392** [US4] `gateway/app/scheduler.py` `_retry_pending`: when both post-launch store writes fail,
  ensure the on-disk park is not left past-due-and-un-landed (persist "launched" durably, or dedupe by run
  key on the next tick).

**Checkpoint**: SC-120 green — no duplicate GPU training runs.

---

## Phase 5: User Story 5 — race-free lifecycle admission (P2)

**Goal**: No unlocked read releases admission mid-load (FR-193). **Test**: interleaved load/unload never
double-admits.

- [ ] **T393** [P] [US5] `tests/test_agent_lifecycle.py` — interleave `ensure_loaded` and hard-cut `unload`
  so the `_loading` read races the flag set; assert admission is never released while a load holds the lock
  and two models never co-reside.
- [ ] **T394** [US5] `hostagent/lifecycle.py` `unload()`: read `_loading` and decide teardown under
  `self.lock`; the failed-acquire hard-cut path must not read the guard or call `admission.release()` outside
  the lock.

**Checkpoint**: SC-121 green.

---

## Phase 6: User Story 6 — single-flight swap reservation (P2)

**Goal**: Concurrent same-target swaps are serialized; `end_swap` never drops a still-held reservation
(FR-194). **Test**: two concurrent `begin_swap("vision")` never overlap.

- [ ] **T395** [P] [US6] `tests/test_agent_swap_txn.py` — two concurrent same-target `begin_swap` calls →
  the second is rejected-or-queued; a completing swap does not clear a reservation the other still holds.
  MUST fail on current `HEAD`.
- [ ] **T396** [US6] `hostagent/admission.py` `begin_swap`/`end_swap`: make the reservation single-flight per
  target and refcount-safe (reject-or-wait a concurrent same-target begin; `end_swap` releases only when the
  last holder finishes).

**Checkpoint**: SC-122 green.

---

## Phase 7: User Story 7 — agent producers match the contracts (P2)

**Goal**: `/health` and `/engines` round-trip through `AgentHealth`/`EngineState` (FR-195). **Test**:
contract `from_json` on live payloads preserves GPU state and validates.

- [ ] **T397** [P] [US7] `tests/test_agent_http.py` — `AgentHealth.from_json(<agent /health body>)` yields
  non-default `gpu_free_gb/holder/holder_kind/wedged`; `EngineState.from_json(<an /engines row>)` validates
  without raising. Both MUST fail on current `HEAD`.
- [ ] **T398** [US7] `hostagent/main.py`: flatten the `/health` GPU fields to top-level
  `gpu_free_gb`/`holder`/`holder_kind`/`wedged`; include `engine_id` in each `/engines` row (and, while
  here, compute `engine_states()` once per `/health` rather than twice).

**Checkpoint**: SC-123 green — producer/contract drift closed.

---

## Phase 8: User Story 8 & 9 — per-engine budgets + total verdict selection (P3)

**Goal**: Per-engine startup/idle tuning (FR-196) and timestamp-independent verdict selection (FR-197).

- [ ] **T399** [P] [US8] `hostagent/adapters/__init__.py` `build_runtimes`: thread a per-engine
  `ready_wait_s` (and per-engine idle timeout) from the registry/env into each `EngineRuntime`, restoring the
  slow-bento cold-start grace; test in `tests/test_agent_adapters.py` that a slow-first-load engine is not
  killed at 60 s and a per-engine idle setting is honored (SC-124).
- [ ] **T400** [P] [US9] `gateway/app/scheduler.py` `_default_shadow`: make newest-verdict selection total —
  break ties on a secondary key (object key / uuid) when `LastModified` is absent, or fail-closed rather than
  silently choose a stale verdict; test in `tests/test_promotion_modes.py` with a listing whose newest object
  omits `LastModified` (SC-125).

**Checkpoint**: SC-124 + SC-125 green.

---

## Final gate

- [ ] **SC-126** Full existing suite green; default (non-preempt, single-tenant, manual-promotion) paths
  behavior-preserving. No new dependency/service/runtime. Constitution v1.4.1 unchanged.

## Dependencies & parallelism

- US1–US4 (P1) are mutually independent — different files — and can be implemented/merged in any order or in
  parallel. US5–US9 likewise touch disjoint files. Within each story, the test task `[P]` is written first
  (must fail), then the fix.
- No task depends on another story's fix; the only ordering is test-before-fix inside a story.
