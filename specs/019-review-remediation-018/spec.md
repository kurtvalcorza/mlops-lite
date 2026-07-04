# Feature Specification: 018 Review Remediation — durability, lease-recovery, swap-safety, and scheduler idempotency

**Feature Branch**: `019-review-remediation-018`

**Created**: 2026-07-03

**Status**: Draft — sourced from a high-effort code review of the 018 platform re-architecture
(`master..HEAD`, the folded-in GPU host agent + policy loop). Ten findings survived independent
verification; this spec turns the confirmed correctness bugs into fixable, independently-testable
work items. Requirement IDs continue the shared space (FR-188+, SC-117+, tasks T382+).

**Input**: A recall-biased review of the 018 stack (host agent, durable journal, policy scheduler,
transactional swap, shared contracts, GPU lease). Findings, with verification verdict:

| # | Finding | Location | Verdict |
|---|---------|----------|---------|
| 1 | Journal loses a fsync'd transition after a torn-tail crash (append concatenates onto the partial line; replay then discards the whole line) | `hostagent/journal.py` | CONFIRMED |
| 2 | GPU lease wedges every *other* tenant with a permanent 409 — an ours-but-wrong-tenant live record can never self-heal | `serving/gpu_lease.py` | CONFIRMED |
| 3 | Swap probe treats any HTTP response (incl. 5xx) as "reachable" → evicts the working holder for a target that can't serve | `gateway/app/swap.py` | CONFIRMED |
| 4 | Non-idempotent retrain launch → duplicate GPU training runs on a lost response or a double store-write failure | `gateway/app/scheduler.py` | CONFIRMED |
| 5 | `unload()` reads `_loading` without the lock → double-admission race (two models on one GPU) | `hostagent/lifecycle.py` | PLAUSIBLE |
| 6 | `begin_swap` admits two concurrent same-target swaps and drops the shared reservation early | `hostagent/admission.py` | CONFIRMED (latent) |
| 7 | Agent `GET /health` nests GPU fields under `gpu`, but the `AgentHealth` contract declares them flat → consumer reads the GPU as free while held | `hostagent/main.py` / `platformlib/contracts.py` | CONFIRMED (latent) |
| 8 | Agent `GET /engines` row shape omits `engine_id`, so `EngineState.from_json` raises `ContractError` on every row | `hostagent/main.py` / `platformlib/contracts.py` | CONFIRMED (latent) |
| 9 | Bento engines fold under a uniform 60 s startup budget with no per-engine override; per-engine idle env vars are ignored | `hostagent/adapters/__init__.py` | PLAUSIBLE |
| 10 | Newest-shadow-verdict selection can't pick a verdict whose `LastModified` is absent → stale verdict drives promotion | `gateway/app/scheduler.py` | PLAUSIBLE (low reachability) |

> **Scope note**: 019 is **remediation only** — it fixes defects introduced by the 018 fold-in and does
> not add a new capability, service, runtime, or dependency. Every fix **preserves** the existing
> behavior contracts and the constitution's Principle II (at most one GPU tenant, no time-of-check /
> time-of-use window). Finding 7/8 fixes align the *producer* (agent handlers) to the *existing*
> `platformlib.contracts` shapes — the contracts do not change.

> **Hard boundary (NON-NEGOTIABLE)**: fixes MUST NOT weaken Principle II. The lifecycle (F5) and
> swap-reservation (F6) fixes *tighten* single-tenant admission; none may open a window where two models
> co-reside or a running training/HPO/batch job is preempted.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — The journal never silently drops a durable transition (Priority: P1)

The host agent records every engine/job lifecycle transition to a fsync'd JSONL journal so that a
restart rebuilds exact state. Today, if the agent crashes mid-write leaving a torn (newline-less) tail
line, the next append concatenates its record onto that partial line, and the following replay discards
the entire malformed line — silently losing a transition that was reported as durable.

**Why this priority**: Silent state loss is the worst failure class here — a completed/failed job reverts
or a running job is mis-counted as interrupted, and the "restart-proof journal" guarantee (the whole
reason the journal exists) is void. No operator signal.

**Independent Test**: Write a valid record, truncate the file to remove its trailing newline (simulating a
torn tail), append a second record, then replay → **both** records survive (or the torn line is provably
repaired and the second record is intact); a follow-up replay is idempotent.

**Acceptance Scenarios**:

1. **Given** a journal whose last line lacks a trailing newline, **When** `_append` writes the next
   record, **Then** the two records occupy two separate, individually-parseable lines (append repairs the
   tail: seek-to-clean-EOF / truncate the partial line, or prefix a newline).
2. **Given** a mid-write crash, **When** the agent restarts and replays, **Then** every record that was
   fsync'd before the crash is reconstructed and no later valid record is lost.
3. **Given** `transition()` is called, **When** the durable append raises (disk full / EIO), **Then** the
   in-memory record is **not** advanced past durable state (memory never leads the log), and the caller
   sees the failure.

---

### User Story 2 — The GPU lease self-recovers instead of wedging other tenants (Priority: P1)

The host agent hosts several tenant identities behind one PID. If a lease release is skipped or fails
(the in-process holder is cleared but the on-disk lockfile still shows this PID holding, say,
`llm-serving`), the next acquisition for a *different* tenant (e.g. `vision`) hits the
ours-but-different-tenant branch and raises `LeaseHeld` — and because the record is "ours and alive" the
self-heal path (which only fires for dead holders) never clears it. Every other GPU engine is refused
with a 409 until the agent restarts.

**Why this priority**: A single dropped release permanently disables all-but-one GPU engine with no
recovery short of a restart — a latent single-point wedge on the platform's core resource.

**Independent Test**: Force a stale ours-but-wrong-tenant lockfile record for the current live PID, then
`acquire()` a different tenant → it succeeds by reclaiming our own stale record (same-PID self-heal),
without a restart; a concurrent *different-PID* live holder is still correctly refused.

**Acceptance Scenarios**:

1. **Given** a lockfile record owned by our live PID for tenant A, **When** the same process acquires
   tenant B, **Then** the acquisition reclaims the stale record and succeeds (same-owner tenant switch is
   self-healing), rather than raising `LeaseHeld`.
2. **Given** a lockfile record owned by a *different* live PID, **When** we acquire, **Then** we are still
   refused (`LeaseHeld`) — the fix must not weaken cross-process exclusion.
3. **Given** `admission.release()` swallows a failed `lease.release()`, **When** the next acquire runs,
   **Then** it does not wedge — the release failure is either surfaced/logged or recovered on the next
   acquire.

---

### User Story 3 — A swap never evicts the resident holder for a target that cannot serve (Priority: P1)

Preemptive swap must verify the target can actually serve *before* evicting the resident GPU holder.
Today the target probe returns `True` for **any** HTTP response — a target daemon that is up but whose
model failed to load (e.g. `/healthz` → 503) passes the probe, the working holder is evicted, and the
forward to the target still fails: both models gone, request 503s anyway.

**Why this priority**: The probe exists specifically to prevent an evict-then-fail outage; as written it
does the opposite — it can convert a one-model-degraded state into a zero-model outage.

**Independent Test**: Stub the target probe endpoint to return 503 (up but not ready) → `preempt_if_needed`
refuses the swap (does not evict the holder) and the resident model keeps serving; a 200 target still
swaps.

**Acceptance Scenarios**:

1. **Given** the swap target returns a non-2xx health status, **When** the probe runs, **Then** it reports
   the target **not** serve-ready and the swap is refused before any eviction.
2. **Given** the swap target returns 200, **When** the probe runs, **Then** it reports reachable and the
   swap proceeds.
3. **Given** the probe reports not-ready, **When** the operator is answered, **Then** the resident holder
   is untouched and the response distinguishes "target not ready" from "holder busy/refused".

---

### User Story 4 — The scheduler never launches a duplicate retrain for one breach (Priority: P1)

A quality/shadow breach parks a retrain and launches it against the trainer. The launch carries no
idempotency key, so two realistic partial failures each fire a **second** identical retrain for the same
breach: (a) the trainer accepts the run but the HTTP response is lost (timeout/reset) → treated as a
launch failure → cooldown released → next due check re-launches; (b) after an accepted launch, both
`clear_pending()` and the fallback `landed` `save_pending()` fail during a store outage → the on-disk park
stays past-due and un-landed → next tick re-launches.

**Why this priority**: A duplicate retrain consumes the single GPU twice and can thrash the promotion
loop; the failure modes are ordinary network/store faults, not exotic.

**Independent Test**: (a) Make the trainer POST raise *after* accepting the run → assert exactly one run is
launched (the breach is not re-fired). (b) Make both post-launch store writes fail → assert the next tick
does not re-launch the already-accepted breach.

**Acceptance Scenarios**:

1. **Given** an accepted launch whose HTTP response is lost, **When** the next due check runs, **Then** no
   second run is dispatched for the same breach (an idempotency/run key lets the trainer or scheduler
   dedupe, or the park is marked launched before the request so a lost response is not read as failure).
2. **Given** both post-launch store writes fail, **When** the next tick runs, **Then** the already-launched
   breach is not re-launched.
3. **Given** a launch that genuinely failed (trainer rejected/unreachable, no run started), **When** the
   next due check runs, **Then** a retry is still allowed (the fix must not suppress a real retry).

---

### User Story 5 — Host-agent admission stays race-free under concurrent load/unload (Priority: P2)

`ensure_loaded()` acquires the runtime lock and only *then* sets `_loading = True`; a concurrent hard-cut
`unload()` whose timed lock acquisition fails reads `_loading` **without** the lock. In the window after
the loader takes the lock but before it sets the flag, the reader sees `False`, proceeds lock-free into
teardown, and can `admission.release()` the slot the loader is about to populate — the double-admission
the flag was added to prevent.

**Why this priority**: It can transiently place two models on one GPU (Principle II violation), but the
window is narrow and hard to hit; correctness-critical, lower reachability than P1.

**Independent Test**: Concurrency test that interleaves `ensure_loaded` and hard-cut `unload` so the flag
read races the flag set → admission is never released while a load holds the lock; no double-admission.

**Acceptance Scenarios**:

1. **Given** a load in progress, **When** a hard-cut unload cannot acquire the lock, **Then** it does not
   read `_loading`/mutate state outside the lock in a way that can release the loader's admission.
2. **Given** the loader sets its guard, **When** it is set relative to the lock, **Then** the set and every
   read of the guard are ordered by the same lock (no unlocked read decides teardown).

---

### User Story 6 — Agent-native swap is single-flight per target (Priority: P2)

`admission.begin_swap(target)` rejects only a swap for a *different* target, and does not hold a lock
across the transaction. Two concurrent swaps for the **same** target both pass and both set
`_swap_target`; the first's `end_swap` unconditionally clears the shared reservation while the second is
mid-transaction, reopening the evict→re-acquire window the reservation exists to close.

**Why this priority**: Latent today (the gateway serializes all swaps via `_loop_swap_lock`); becomes live
when the agent-native swap path (T363) is exposed to concurrent same-target requests.

**Independent Test**: Two concurrent `begin_swap("vision")` calls → the second is rejected or made to wait;
`end_swap` does not release a reservation another in-flight swap still holds (refcount or single-flight).

**Acceptance Scenarios**:

1. **Given** a swap for target T is in flight, **When** a second swap for the same T begins, **Then** it is
   serialized (rejected-and-retry or queued) — never two overlapping transactions on one target.
2. **Given** overlapping swaps were (incorrectly) admitted, **When** one completes, **Then** it does not
   clear a reservation the other still relies on.

---

### User Story 7 — Agent `/health` and `/engines` match the platformlib contracts (Priority: P2)

The `AgentHealth` and `EngineState` contracts in `platformlib/contracts.py` were introduced alongside the
agent producers but disagree with them: `/health` nests GPU fields under a `gpu` object (and names the
inner key `free_gb`, not `gpu_free_gb`) while the contract declares them flat, so a contract consumer
reads the GPU as free/unheld; `/engines` rows omit `engine_id` (it is only the map key), so
`EngineState.from_json` raises `ContractError` on every row.

**Why this priority**: Latent (no consumer parses via the contract yet), but the first consumer wired in
silently gets empty GPU state or a hard parse failure — a trap laid for the next increment.

**Independent Test**: `AgentHealth.from_json(<real /health body>)` round-trips the GPU fields (not
defaults); `EngineState.from_json(<a real /engines row>)` validates without raising.

**Acceptance Scenarios**:

1. **Given** the live `/health` body, **When** parsed via `AgentHealth.from_json`, **Then** `gpu_free_gb`,
   `holder`, `holder_kind`, `wedged` reflect the real GPU state (producer flattened to match the contract).
2. **Given** a live `/engines` row, **When** parsed via `EngineState.from_json`, **Then** it validates —
   the row carries `engine_id` (producer includes it in the value).

---

### User Story 8 — Engine startup and idle budgets are per-engine configurable (Priority: P3)

`build_runtimes` never passes `ready_wait_s`, so every folded-in engine inherits the 60 s lifecycle
default, and idle timeout honors only the global `IDLE_TIMEOUT`. The slow BentoML engines (vision / embed
/ tabular) previously had a larger cold-start grace and per-engine idle tuning; a fresh-host first load
that imports BentoML and downloads weights can exceed 60 s, get killed, and retry the download from
scratch — a slow-first-load engine can 503 indefinitely with no config seam.

**Why this priority**: Availability/tuning regression, not a correctness bug; recoverable by config once
the seam exists.

**Independent Test**: Configure a per-engine `ready_wait_s` (or restore the per-engine grace table) → a
slow bento engine whose first load needs > 60 s becomes ready instead of being killed; the default
(fast) engines are unchanged.

**Acceptance Scenarios**:

1. **Given** a bento engine with a legitimately slow first load, **When** it is wired via `build_runtimes`,
   **Then** it is granted its per-engine startup budget rather than a uniform 60 s.
2. **Given** a deployment that set a per-engine idle timeout, **When** the reaper runs, **Then** that
   engine honors its configured idle window (the per-engine env is not silently ignored).

---

### User Story 9 — Shadow-verdict selection is robust to a missing timestamp (Priority: P3)

The newest-matching-shadow-verdict tie-break (`best_at is None or (modified is not None and modified >
best_at)`) can never replace a chosen real-timestamped verdict with a genuinely-newer one whose
`LastModified` is absent, so a stale verdict can drive promotion. Real S3 always populates `LastModified`;
this fires only against a non-conforming store/mock.

**Why this priority**: Low reachability (production S3 is conformant), but a wrong promotion decision is
high-consequence; the selection should be total, not dependent on an always-present field.

**Independent Test**: A verdict listing where the newest matching object has no `LastModified` → selection
still picks the genuinely-newest matching verdict by a total ordering (e.g. secondary key), not a stale
one.

**Acceptance Scenarios**:

1. **Given** a listing missing `LastModified` on the newest matching verdict, **When** selection runs,
   **Then** it does not silently pick an older verdict (fail-closed / secondary ordering / explicit error
   rather than a stale silent choice).

### Edge Cases

- Journal: an empty file, a file that is *only* a torn line, and a torn line whose partial bytes happen to
  parse as valid-but-truncated JSON.
- Lease: same-PID reuse across a `fork`/exec boundary where the PID is reused (start-time check must still
  discriminate).
- Swap probe: a target that returns 200 on `/healthz` but is not model-ready (probe the readiness surface,
  not merely liveness).
- Scheduler: a launch that partially succeeds (run created) but the trainer later reports it never
  started — idempotency key must reconcile, not double-count.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-188**: The journal MUST guarantee that any transition durably appended before a crash survives
  replay — a torn tail line MUST NOT cause loss of the next valid record (append repairs the tail before
  writing). (Finding 1)
- **FR-189**: `transition()`/`submit()` MUST NOT advance in-memory state past what has been durably
  appended — a failed append MUST leave memory consistent with the log and surface the failure. (Finding 1)
- **FR-190**: The GPU lease MUST let the owning process reclaim its own stale record on a tenant switch
  (same-owner self-heal) so a dropped/failed release cannot permanently wedge other tenants, while
  continuing to refuse a *different* live owner. (Finding 2)
- **FR-191**: The swap target probe MUST treat only a serve-ready response (2xx / explicit readiness) as
  "target reachable"; a non-2xx (e.g. 503 up-but-not-loaded) MUST refuse the swap **before** evicting the
  resident holder. (Finding 3)
- **FR-192**: A retrain launch MUST be idempotent per breach: a lost response or a post-launch store-write
  failure MUST NOT cause a second training run for the same breach; a launch that never started a run MUST
  still be retriable. (Finding 4)
- **FR-193**: Host-agent admission state (`_loading`/child/reservation) MUST be read and written under a
  single lock discipline — no unlocked read may decide teardown or release admission during an in-progress
  load. (Finding 5)
- **FR-194**: Agent-native swap MUST be single-flight per target — two concurrent swaps for the same
  target MUST be serialized, and a swap's completion MUST NOT release a reservation another in-flight swap
  still holds. (Finding 6)
- **FR-195**: The agent `GET /health` payload MUST conform to `platformlib.contracts.AgentHealth`
  (flat `gpu_free_gb`/`holder`/`holder_kind`/`wedged`), and `GET /engines` rows MUST conform to
  `EngineState` (each row carries `engine_id`); a round-trip through the contract MUST NOT lose GPU state
  or raise `ContractError`. (Findings 7, 8)
- **FR-196**: Folded-in engines MUST support a per-engine startup budget (`ready_wait_s`) and per-engine
  idle timeout; a slow-first-load engine MUST NOT be killed by a uniform 60 s default, and a per-engine
  idle setting MUST NOT be silently ignored. (Finding 9)
- **FR-197**: Newest-shadow-verdict selection MUST be total — it MUST NOT depend on an always-present
  `LastModified`; a missing timestamp MUST NOT cause a silently-stale verdict to drive promotion.
  (Finding 10)

### Key Entities

- **Journal record**: one JSONL line, `{ts, job/engine id, from, to, ...}`; the durability unit. Invariant:
  every physical line is independently parseable; memory never leads the durable log.
- **Lease record**: the lockfile entry `{pid, start_time, tenant}`; ownership is `(pid, start_time)`,
  tenant is switchable by the owner.
- **Swap reservation**: `admission._swap_target`; must be single-flight and refcount-safe per target.
- **AgentHealth / EngineState**: the `platformlib.contracts` shapes the agent producers must satisfy.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-117**: A torn-tail-then-append-then-replay sequence loses **zero** durably-appended records
  (regression test, Finding 1). (FR-188/189)
- **SC-118**: After a dropped/failed lease release, the next acquire for a different tenant succeeds
  without an agent restart, and a different-PID live holder is still refused (regression test, Finding 2).
  (FR-190)
- **SC-119**: A swap against a 503 (up-but-not-ready) target evicts **nothing** and the resident model
  keeps serving; a 200 target still swaps (regression test, Finding 3). (FR-191)
- **SC-120**: Under a lost launch response and under a double store-write failure, **exactly one** training
  run is launched per breach; a genuinely-failed launch is still retried (regression tests, Finding 4).
  (FR-192)
- **SC-121**: A load/unload interleaving stress test never releases admission during an in-progress load
  and never co-resides two models (regression test, Finding 5). (FR-193)
- **SC-122**: Two concurrent same-target `begin_swap` calls never run overlapping transactions and
  `end_swap` never drops a still-needed reservation (regression test, Finding 6). (FR-194)
- **SC-123**: `AgentHealth.from_json(<live /health>)` preserves all four GPU fields and
  `EngineState.from_json(<live /engines row>)` validates without raising (regression tests, Findings 7/8).
  (FR-195)
- **SC-124**: A bento engine whose first load exceeds 60 s becomes ready via a per-engine budget instead of
  being killed; a per-engine idle setting is honored (test, Finding 9). (FR-196)
- **SC-125**: Newest-verdict selection with a missing `LastModified` on the newest object does not pick a
  stale verdict (regression test, Finding 10). (FR-197)
- **SC-126**: No regression — the full existing test suite stays green; the default (non-preempt,
  single-tenant) paths are behavior-preserving.

## Assumptions

- The fixes are behavior-preserving on the happy path; only the failure/edge paths change. The
  constitution's Principle II (one GPU tenant, no TOCTOU) is **strengthened**, never weakened.
- Findings 6/7/8 are latent (no live consumer/path yet); they are fixed now because the producer and the
  same-PR contract were meant to agree and the next increment will wire the consumer.
- Finding 10 targets a non-conforming store; the fix makes selection total rather than assuming S3's
  always-present `LastModified`.
- No new dependency, service, or runtime is introduced (Principle III, frozen GPU stack untouched).
