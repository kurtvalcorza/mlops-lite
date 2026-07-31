# Contract — GPU Coordinator & Scheduler (internal, host agent)

Not a network API — the internal contract for the **GPU coordinator** that replaces the single-slot
`hostagent/admission.py` design, and the scheduler that feeds it. This is the enforcement point for
Principle II (constitution v1.6.1). **Revised twice.** Round 1 (Codex architecture review) fixed:
(1) never hold the lock across child lifecycle ops (the ABBA lesson already in `admission.py`),
(2) correct VRAM accounting (no double-count), (3) single GPU-scheduling authority.

**Round 2 (PR #74 review)** fixed four defects that round 1 introduced or left open:
(a) stage 1 marked eviction victims and then reserved in the same pass, so a reservation could be
recorded while the victim was still resident and accounted — now **evict-then-recompute**;
(b) the live-free bound ignored concurrent not-yet-materialized loads, letting two reservations claim
the same free bytes — now deducted;
(c) the stale-generation rollback called `unload()` *inside* the lock, reintroducing the very ABBA
deadlock (1) removed — now split into record-intent / unload-outside / finalize;
(d) `gateway/app/scheduler.py` was misidentified as a competing GPU-ordering authority; it is the 018
`PolicyScheduler`. Corrected, and the actual gap — tenant-less policy retrains bypassing the jobs
lane — is now addressed.

**Round 3 (PR #74 re-review)** closes the defects round 2 left open or introduced:
(e) the `active_requests` ref-count had no balanced lifecycle — `Share` incremented with no defined
release, and a cold load reached `Grant` without ever incrementing, so a freshly loaded model was
observable as idle and evictable while its triggering request ran. Now an explicit **claim**;
(f) a *failed* load (spawn error, timeout, OOM, missing model) never cleared its reservation or
`loading` state, leaking capacity permanently and stranding every later request for that model behind
an in-flight load that no longer exists. Now an explicit failure path;
(g) `draining`/`evicting`/`rolling_back` matched no branch and fell through to a fresh load, letting a
new `loading` entry be created for a model an owning operation was mid-way through removing;
(h) the post-load drift check compared against *this* load's real bytes while silently dropping every
other outstanding reservation, so it could commit a load that breaks invariant 1;
(i) `select_victims` excluded only `model_key`, so two sequential attempts could each mark the same
victim and race two `unload()`s against each other;
(j) a device-wide free-memory delta is not attributable when two loads run concurrently — now measured
per child PID;
(k) `admit_job` overwrote an existing `exclusive_job` rather than refusing;
(l) transient contention was reported as the permanent-looking `model_too_large`.

**Still open — deliberately not resolved here** (they are design decisions, not corrections): the
`job_barrier` drain-convergence and the TOCTOU for reservations already past stage 1 when the barrier
rises. See *Jobs (exclusive)* below.

## Why a coordinator, not an extended lease
Today `admission.py` is structurally **single-slot** (`_holder`, `_swap_target`) and its own comments record
that **holding the RLock across evict→free→load deadlocked ABBA**. Co-residency needs a *set* of residents
with per-model lifecycle, active-request ref-counts, reservations, and rollback — a small **state machine**,
not more fields on the single-slot lease.

## Coordinator state

```
residents: model_key -> ResidentModel {
    state: loading | resident | draining | evicting | rolling_back
    vram_accounted_bytes            # reservation estimate, reconciled to real delta after load
    active_requests: int            # ref-count; eviction waits for 0
    last_used_at
}
exclusive_job: None | { job_id, started_at }     # at most one; whole GPU
job_barrier: bool                                 # set during job drain; refuses NEW serving admits
reservations: op_id -> { model_key, est_bytes, generation, materialized: bool }
                                                  # idempotent, keyed by request/job id
                                                  # materialized=false until reconciled to a real delta
generation: monotonically increasing token per model_key (guards commit/rollback)
lock: RLock                                       # guards STATE ONLY, never held across lifecycle I/O
load_gate: Semaphore                              # serializes load+measure so a per-PID reading is
                                                  # attributable; NEVER taken while `lock` is held

# Tunables (defaults defined in .specify/memory/hardware-profile.md):
#   safety_reserve, safety_headroom, max_admission_attempts,
#   drain_timeout, job_drain_timeout, admission_backoff
```

**Resource identity is the model/runtime instance, not the tenant** — many tenants requesting the same model
share one resident child (ref-count rises); the child is not duplicated per tenant.

## VRAM admission (evict-then-recompute; two distinct bounds)

A reservation is **never** recorded against memory that has not actually been freed. Stage 1 either
finds room *as it stands today*, or it completes an eviction and **re-derives both bounds from
scratch** on the next attempt. It never marks a victim and then reserves in the same pass.

```
usable_capacity = min(configured_budget, NVML_total - safety_reserve)
unmaterialized  = Σ est_bytes of reservations whose load has not yet been reconciled to a real delta

admit_serving(model_key, est_bytes, deadline):
  for attempt in 1..max_admission_attempts:        # bounded; never an unbounded spin
    # ---- stage 1: decide under lock, perform NO I/O ----
    under lock:
      if exclusive_job or job_barrier:  return Refuse(gpu_busy, retry_after)
      switch residents[model_key].state:
        resident:
            active_requests += 1; return Share(claim)           # shared child, no new VRAM
        loading:
            plan = AwaitLoad                                    # coalesce; never double-load
        draining | evicting | rolling_back:
            plan = AwaitTransient   # an owning op is mid-flight; do NOT create a competing entry
        absent:
          accounted      = Σ residents.vram_accounted + Σ reservations.est_bytes
          effective_free = NVML.live_free() - unmaterialized    # concurrent loads deducted
          fits_budget    = accounted + est_bytes <= usable_capacity
          fits_live      = est_bytes <= effective_free - safety_headroom
          if fits_budget and fits_live:
              record reservation{op_id, model_key, est_bytes, generation}
              residents[model_key].state = loading
              plan = Load
          else:
              victims = select_victims(idle first, then LRU,
                                       state == resident only,   # never re-target an in-flight op
                                       excluding model_key)
                        sufficient to satisfy BOTH bounds
              if victims empty:
                  # Nothing evictable. Distinguish PERMANENT from TRANSIENT before answering:
                  if est_bytes > usable_capacity - safety_headroom:
                      return Refuse(model_too_large)   # 413 — cannot fit even on an empty GPU
                  return Refuse(gpu_busy, retry_after) # 503/429 — outstanding reservations or an
                                                       # unaccounted external consumer; retryable
              mark each victim 'draining'; record evict_intent{op_id, victims}
              plan = EvictThenRetry
    # ---- stage 2: I/O strictly OUTSIDE the lock ----
    case plan:
      AwaitLoad:       await the in-flight load (bounded by deadline)
                       on success: the loader hands this waiter a claim (see Request claims); return Share(claim)
                       on failure: continue                     # the loader cleaned up; re-evaluate
      AwaitTransient:  await the owning op to finalize (bounded by deadline); continue
      EvictThenRetry:  evict_all(victims)      # completes: drain -> unload -> NVML verified
                       continue                # ← next attempt re-derives BOTH bounds
      Load:            proceed to stage 3
  return Refuse(gpu_busy, retry_after)         # attempts exhausted

  # ---- stage 3: load outside the lock, then commit ----
  hold load_gate:                               # serialized so the per-PID reading is attributable
    try:
      child      = spawn/load(model_key)
      real_bytes = NVML.used_by_pid(child.pid)  # per-PROCESS, not a device-wide delta
    except spawn failure | readiness timeout | OOM | model missing:
      under lock:
        if generation unchanged since reservation:
            drop reservation; remove residents[model_key]; bump generation
        # else: an eviction already reclaimed the slot — nothing of ours remains to clear
      unload(partial child) if one was spawned  # ← OUTSIDE the lock
      fail every AwaitLoad waiter on this model_key with the same outcome
      return Refuse(load_failed)                # capacity is NOT left reserved

  under lock:
    stale  = generation changed since reservation (model evicted meanwhile)
    others = Σ est_bytes of reservations OTHER than this op        # ← retained, not dropped
    drift  = Σ residents.vram_accounted + real_bytes + others > usable_capacity
    if stale or drift:
        residents[model_key].state = rolling_back      # record INTENT only
        drop reservation
        rollback = true
    else:
        residents[model_key] = resident, vram_accounted = real_bytes
        active_requests += 1 + (number of AwaitLoad waiters joining)   # ← claims, atomic with commit
        drop reservation
  if rollback:                                  # ← unload happens OUTSIDE the lock
    unload(child); verify NVML free rose
    under lock: remove residents[model_key]; bump generation
    return Retry if stale
           else Refuse(model_too_large if real_bytes > usable_capacity - safety_headroom
                       else gpu_busy)           # drift caused by contention is retryable
  return Grant(claim)
```

**Why the rollback is split.** Unloading inside the commit critical section would hold the coordinator
lock across child lifecycle I/O — precisely the ABBA deadlock this redesign exists to remove, and the
one `admission.py`'s own comments record. The lock records the *decision*; the unload happens after
release; a final short critical section finalizes state.

**Why measurement is per-PID.** Two reserved models can load concurrently, so a device-wide pre/post
free-memory delta is not attributable to either: each reading may include the other's allocation or
catch only part of it, which double-accounts one model and under-accounts the other — the second
outcome admits later work against VRAM that is not actually free. Every resident is its own child
process, so `NVML.used_by_pid(child.pid)` is both exact and naturally scoped. `load_gate` serializes
load+measure so the reading is taken at a quiescent moment; it is a *lifecycle* gate and is never
taken while the state lock is held (the ABBA constraint applies to it identically).

## Request claims (the `active_requests` ref-count)

Every admission that returns `Share` or `Grant` returns a **claim**. Claims are what keep a resident
alive: eviction drains on `active_requests == 0`, so an unbalanced count is either a permanent
eviction block (leak) or an eviction racing a live request (early release).

- **`Share`** increments inside the same critical section that observed `resident` — never after.
- **`Grant`** increments in stage 3's commit section, *atomically with* `state = resident`. The
  triggering request is therefore never observable as idle-with-zero-requests between commit and first
  use, which is exactly the window in which a concurrent admission would have picked it as an idle
  victim and unloaded a model that was already serving a request.
- **`AwaitLoad` waiters** are handed their claim by the loader in that same commit section. A waiter
  never increments on its own afterwards; if the load fails, every waiter fails with it.
- **Release is mandatory and `finally`-style.** Every terminal path — success, error, client
  disconnect, deadline — releases exactly once, under lock, updating `last_used_at`.

Four invariants (assert-tested):

1. `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity` — bounds the accounted set.
2. Every individual load fits `live_free − unmaterialized − safety_headroom` — bounds the incoming
   load against *instantaneous* free memory, with concurrent not-yet-visible loads deducted so two
   reservations cannot both claim the same free bytes.
3. No reservation exists that is backed by a victim still resident — eviction completes before the
   replacement reservation is recorded.
4. `active_requests` equals the number of outstanding claims for that model, and is never negative.
   A model in `resident` state with `active_requests == 0` is genuinely idle and evictable.

Invariants 1 and 2 are **distinct**: the first is a budget accounting bound, the second a physical
memory bound that also holds when *unaccounted external* GPU consumers are present.

## Eviction (safe — never mid-request, bounded wait)
```
evict(victim, deadline) :
  under lock: victim.state = draining        # stop admitting NEW requests to it
  wait until victim.active_requests == 0     # OUTSIDE lock, bounded by drain_timeout
      on timeout: under lock: victim.state = resident   # revert; victim was NOT freed
                  return EvictFailed(busy)
  under lock: victim.state = evicting        # drained; committed to unload
  unload(victim); verify NVML free rose      # OUTSIDE lock
  under lock: remove victim; bump generation
  return Evicted
```
Idle-first, then LRU. **Eviction never interrupts in-flight requests.**

**Caller contract when blocked behind an eviction.** A request whose `EvictThenRetry` returns
`EvictFailed` does not spin: it consumes one of its `max_admission_attempts`, backs off
(exponential, jittered, capped), and re-enters stage 1 — where the victim is once again `resident`
and may no longer be the best choice. When attempts or `deadline` are exhausted the request is
refused `gpu_busy` with `Retry-After`. **No admission path waits unbounded on another tenant's
in-flight request.**

## Jobs (exclusive)
```
admit_job(job) :
  under lock:
    if exclusive_job or job_barrier:         # ← another job owns the GPU or the transition
        return Wait(retry_after)             # exactly one owner; never overwrite a live claim
    job_barrier = true                       # ← closes the door FIRST; no new serving reservations
    mark all idle residents 'draining'
  drain + unload (outside lock) until serving set empty, bounded by job_drain_timeout
      on timeout: under lock: job_barrier = false; return Wait(retry_after)   # release the door
  under lock:
    assert residents empty and reservations empty      # see OPEN below — this can still race
    exclusive_job = job                                # whole GPU; blocks all co-residency
    job_barrier = false                                # exclusive_job now blocks admission
  # NEVER preempted (FR-010, FR-023a); on end: under lock exclusive_job = None
```
`job_barrier` closes serving admission **before** the drain starts. Without it, a serving reservation
granted during the drain window could materialize between "serving set empty" and setting
`exclusive_job`, so the job would start with a co-resident tenant — violating the exclusivity the
whole jobs lane depends on.

The ownership check is what makes "at most one exclusive job" true rather than merely intended:
without it a second `admit_job` arriving against an already-empty serving set overwrites the first
job's claim, both workloads run, and whichever finishes first clears the other's ownership.

> **OPEN — two design decisions, deliberately unresolved.** Both concern the barrier's *drain*, not
> its entry gate, and each admits more than one defensible answer; they are recorded here rather than
> settled unilaterally, and must be closed before `/speckit-implement`.
>
> 1. **Convergence.** "Mark all idle residents `draining`" is a one-shot pass, but the exit condition
>    is *serving set empty*. A resident that is busy at that instant is correctly not interrupted —
>    but nothing re-marks it once its last request finishes, so it stays `resident` forever and the
>    drain can only ever hit `job_drain_timeout`. Options: make eviction **barrier-driven** (while
>    `job_barrier` holds, every resident transitions to `draining` as soon as it becomes eligible), or
>    re-scan on each release. This is a *liveness* bug, not a safety one.
> 2. **TOCTOU for in-flight reservations.** The barrier gates stage 1, so no *new* reservation is
>    granted — but a request already past stage 1 when the barrier rose is still loading, and stage 3's
>    commit does not re-check `job_barrier`/`exclusive_job` before writing a new resident. The closing
>    `assert` can therefore fire with no defined recovery, or the load commits after the job is
>    granted — the exact co-residency the barrier exists to prevent. Options: have the drain wait on
>    `reservations` to empty as well as `residents`, or have stage 3's commit re-check the barrier and
>    roll back if it is set. These differ in which side pays the latency, which is why it is a choice.

## Scheduler — single authority, bounded fairness
- **The host-agent coordinator is the SOLE GPU-ordering authority.** Every path that can occupy the
  GPU enters through `admit_serving` or `admit_job`; nothing else orders GPU work.
- **`gateway/app/scheduler.py` is NOT a competing authority and is not being retired.** It is
  `PolicyScheduler` from feature 018: a drift/quality monitoring tick loop that launches retrains and
  parks a `PendingRetrain` when the host agent answers `409 Busy` (FR-182). It contains no lane
  ordering, no VRAM admission, and no cross-tenant queue — it *reacts* to GPU contention rather than
  arbitrating it. Reducing it to a status/routing facade would delete the monitoring→retrain feedback
  loop while consolidating no scheduling. It is kept as-is.
- **Policy-triggered retrains are tenant-less jobs and MUST enter the jobs lane.** This is the real
  single-authority gap: today `PolicyScheduler` calls the host agent's `/train` directly, outside any
  lane. Under this design it instead enqueues onto `jobs_lane` under a reserved **system tenant**,
  so that:
  - retrains queue FIFO behind (and ahead of) tenant jobs by arrival, with no privileged bypass;
  - the single `exclusive_job` slot is honoured — a retrain can never co-run with a tenant job;
  - its GPU-seconds are metered to the system tenant rather than being invisible;
  - `PolicyScheduler`'s existing `Busy`/park-and-retry path still works unchanged, because a full
    jobs lane returns the same `409` contract it already handles (FR-182 queue-of-one preserved).
- **inference_lane**: admitted against the budget as it fits (interleaved). Favored over a waiting job…
- **jobs_lane**: strict **FIFO**, persisted in Postgres (survives host-agent restart — Codex risk #5).
- **Bounded inference preference (anti-starvation)**: after a configurable inference burst **or** a head-job
  wait bound, the coordinator enters **job-drain mode** — it stops admitting *new* inference (running
  requests finish, never preempted) until the head FIFO job acquires the GPU. Replaces the v1 "starved
  warning" (which did not actually prevent starvation).
- **Owner override**: pin/pause/reorder queued jobs (FR-025); never preempts a running job.

## Metering hook (reserve → settle, not record-before)
GPU-seconds are **reserved** (estimated/max) at admission and **settled** to the actual on completion — see
jobs-api / inference contracts. A settlement that cannot be persisted is retried via outbox/WAL; admission is
refused only when a *reservation* cannot be recorded (fail-safe, FR-016).

## Traceability
FR-006, FR-009, FR-010, FR-019, FR-023, FR-023a, FR-024, FR-025; SC-006, SC-010. Supersedes the v1 single-
critical-section pseudocode rejected by the Codex review (lock-across-lifecycle + Σ≤live_free double-count).
