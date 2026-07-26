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
      if residents[model_key].state == resident:
          bump active_requests; return Share                    # shared child, no new VRAM
      if residents[model_key].state == loading:
          plan = AwaitLoad                                      # coalesce; never double-load
      else:
          accounted      = Σ residents.vram_accounted + Σ reservations.est_bytes
          effective_free = NVML.live_free() - unmaterialized    # concurrent loads deducted
          fits_budget    = accounted + est_bytes <= usable_capacity
          fits_live      = est_bytes <= effective_free - safety_headroom
          if fits_budget and fits_live:
              record reservation{op_id, model_key, est_bytes, generation}
              residents[model_key].state = loading
              plan = Load
          else:
              victims = select_victims(idle first, then LRU, excluding model_key)
                        sufficient to satisfy BOTH bounds
              if victims empty:  return Refuse(model_too_large)  # nothing to free — genuine
              mark each victim 'draining'; record evict_intent{op_id, victims}
              plan = EvictThenRetry
    # ---- stage 2: I/O strictly OUTSIDE the lock ----
    case plan:
      AwaitLoad:       await the in-flight load (bounded by deadline); continue   # re-evaluate
      EvictThenRetry:  evict_all(victims)      # completes: drain -> unload -> NVML verified
                       continue                # ← next attempt re-derives BOTH bounds
      Load:            proceed to stage 3
  return Refuse(gpu_busy, retry_after)         # attempts exhausted

  # ---- stage 3: load outside the lock, then commit ----
  child      = spawn/load(model_key)
  real_bytes = measure_post_load_delta()
  under lock:
    stale = generation changed since reservation (model evicted meanwhile)
    drift = Σ residents.vram_accounted + real_bytes > usable_capacity
    if stale or drift:
        residents[model_key].state = rolling_back      # record INTENT only
        drop reservation
        rollback = true
    else:
        residents[model_key] = resident, vram_accounted = real_bytes
        drop reservation
  if rollback:                                  # ← unload happens OUTSIDE the lock
    unload(child); verify NVML free rose
    under lock: remove residents[model_key]; bump generation
    return Retry if stale else Refuse(model_too_large)
  return Grant
```

**Why the rollback is split.** Unloading inside the commit critical section would hold the coordinator
lock across child lifecycle I/O — precisely the ABBA deadlock this redesign exists to remove, and the
one `admission.py`'s own comments record. The lock records the *decision*; the unload happens after
release; a final short critical section finalizes state.

Three invariants (assert-tested):

1. `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity` — bounds the accounted set.
2. Every individual load fits `live_free − unmaterialized − safety_headroom` — bounds the incoming
   load against *instantaneous* free memory, with concurrent not-yet-visible loads deducted so two
   reservations cannot both claim the same free bytes.
3. No reservation exists that is backed by a victim still resident — eviction completes before the
   replacement reservation is recorded.

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
  under lock: job_barrier = true             # ← closes the door FIRST; no new serving reservations
              mark all idle residents 'draining'
  drain + unload (outside lock) until serving set empty, bounded by job_drain_timeout
      on timeout: under lock: job_barrier = false; return Wait(retry_after)   # release the door
  under lock:
    assert residents empty and reservations empty      # barrier held, so this cannot race
    exclusive_job = job                                # whole GPU; blocks all co-residency
    job_barrier = false                                # exclusive_job now blocks admission
  # NEVER preempted (FR-010, FR-023a); on end: under lock exclusive_job = None
```
`job_barrier` closes serving admission **before** the drain starts. Without it, a serving reservation
granted during the drain window could materialize between "serving set empty" and setting
`exclusive_job`, so the job would start with a co-resident tenant — violating the exclusivity the
whole jobs lane depends on.

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
