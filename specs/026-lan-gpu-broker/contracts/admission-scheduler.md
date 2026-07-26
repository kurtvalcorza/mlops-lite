# Contract — GPU Coordinator & Scheduler (internal, host agent)

Not a network API — the internal contract for the **GPU coordinator** that replaces the single-slot
`hostagent/admission.py` design, and the scheduler that feeds it. This is the enforcement point for
Principle II (constitution v1.6.1). **Revised after the Codex architecture review** to fix three confirmed
defects: (1) never hold the lock across child lifecycle ops (the ABBA lesson already in `admission.py`),
(2) correct VRAM accounting (no double-count), (3) single GPU-scheduling authority.

## Why a coordinator, not an extended lease
Today `admission.py` is structurally **single-slot** (`_holder`, `_swap_target`) and its own comments record
that **holding the RLock across evict→free→load deadlocked ABBA**. Co-residency needs a *set* of residents
with per-model lifecycle, active-request ref-counts, reservations, and rollback — a small **state machine**,
not more fields on the single-slot lease.

## Coordinator state

```
residents: model_key -> ResidentModel {
    state: loading | resident | draining | evicting
    vram_accounted_bytes            # reservation estimate, reconciled to real delta after load
    active_requests: int            # ref-count; eviction waits for 0
    last_used_at
}
exclusive_job: None | { job_id, started_at }     # at most one; whole GPU
reservations: op_id -> { model_key, est_bytes, generation }   # idempotent, keyed by request/job id
generation: monotonically increasing token per model_key (guards commit/rollback)
lock: RLock                                       # guards STATE ONLY, never held across lifecycle I/O
```

**Resource identity is the model/runtime instance, not the tenant** — many tenants requesting the same model
share one resident child (ref-count rises); the child is not duplicated per tenant.

## VRAM admission (corrected — two distinct checks)

```
usable_capacity = min(configured_budget, NVML_total - safety_reserve)
admit_serving(model_key, est_bytes) under a THREE-STAGE protocol:
  # stage 1 — reserve (under lock, no I/O)
  under lock:
    if exclusive_job: return Refuse(gpu_busy)
    if model_key resident: bump active_requests; return Share            # shared child
    accounted = Σ residents.vram_accounted + Σ reservations.est_bytes
    if accounted + est_bytes > usable_capacity: mark idle/LRU victims to 'draining' (see eviction)
    live_free = NVML.live_free()
    if est_bytes > live_free - safety_headroom: return Refuse(model_too_large)   # incoming load bound
    record reservation{op_id, model_key, est_bytes, generation}; residents[model_key].state = loading
  # stage 2 — load OUTSIDE the lock (established lock order; may take seconds)
  child = spawn/load(model_key)
  real_bytes = measure_post_load_delta()
  # stage 3 — commit or roll back (under lock)
  under lock:
    if generation stale (evicted meanwhile): unload(child); drop reservation; retry or Refuse
    residents[model_key] = resident, vram_accounted = real_bytes; drop reservation
    if Σ accounted now exceeds usable_capacity (estimate drifted): roll back this load; Refuse
  return Grant
```

Two invariants (assert-tested): `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity` **and** every
individual load fit `live_free - headroom`. These are **distinct** — the first bounds the accounted set, the
second bounds the incoming load against instantaneous free memory. (This corrects the v1.6.0 `Σ ≤ live_free`
double-count.)

## Eviction (safe — never mid-request)
```
evict(victim) :
  under lock: victim.state = draining        # stop admitting NEW requests to it
  wait until victim.active_requests == 0     # OUTSIDE lock
  unload(victim); verify NVML free rose      # OUTSIDE lock
  under lock: remove victim; bump generation
```
Idle-first, then LRU. **Eviction never interrupts in-flight requests** (Codex risk #2).

## Jobs (exclusive)
```
admit_job(job) :
  under lock: if residents non-empty -> mark all idle 'draining'; if any active -> Wait
  drain (outside lock) until serving set empty
  under lock: exclusive_job = job            # whole GPU; blocks all co-residency
  # NEVER preempted (FR-010, FR-023a); on end: under lock exclusive_job=None
```

## Scheduler — single authority, bounded fairness
- **The host-agent coordinator is the SOLE GPU-ordering authority.** `gateway/app/scheduler.py` is audited
  and reduced to a **status/routing facade** (no independent GPU ordering) — see plan.
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
