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

**Round 4 (PR #74 re-review)** closes the corner the round-3 claim lifecycle itself opened:
(m) `AwaitLoad` joiners were well-handled on the success and spawn-exception paths but **silently
abandoned by the commit-time rollback** (drift, and nominally stale) — and `drift` is an ordinary
concurrent-load outcome, not a hypothetical, so a joiner was left with no signal until its own deadline;
(n) no **waiter registry** was ever defined, yet both `:154`'s "waiters joining" and `:141`'s "fail every
waiter" require enumerating exactly who awaits a given `model_key` — there was no structure to enumerate;
(o) `AwaitLoad` had no **deadline-exceeded** branch, and its "on failure: continue" contradicted the
failure path's "fail every waiter with the same outcome"; a waiter that had given up but not deregistered
could still be counted among the joiners at commit and handed a claim it would never release — precisely
the invariant-4 leak round 3 exists to close.

**Round 5 (owner decision)** closes the two items rounds 2–4 deliberately left open, because they were
design choices rather than corrections:
(p) **drain convergence** — the drain marked only *idle* residents while its exit condition was *serving
set empty*, so a resident busy at that instant was never re-marked and the drain could only ever time out.
Now **every** resident is marked; the barrier already refuses new admissions, so the counts strictly
decrease and the one-shot pass converges without a hook or a re-scan;
(q) **the in-flight-load TOCTOU** — a load already past stage 1 when the barrier rose could commit a new
resident after the drain saw an empty set. Stage 3's commit now re-checks the barrier and rolls back,
refusing the request with retryable `gpu_busy`. Together these make the closing `assert residents empty
and reservations empty` provable rather than hopeful.

**Nothing in this contract is now marked OPEN.** See *Jobs (exclusive)* for both decisions and the
trade-off each accepts.

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
reservations: op_id -> { model_key, est_bytes, generation, materialized: bool,
                         waiters: ordered set of waiter handles }
                                                  # idempotent, keyed by request/job id
                                                  # materialized=false until reconciled to a real delta
                                                  # waiters = the AwaitLoad joiners this load owns; the
                                                  #   single-flight rule means at most ONE reservation per
                                                  #   model_key is in `loading`, so it is unambiguous which
                                                  #   load a joiner is registered against
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
            register op_id in the in-flight load's reservation.waiters   # ← under THIS lock
            plan = AwaitLoad                                    # coalesce; never double-load
        draining | evicting | rolling_back:
            plan = AwaitTransient   # an owning op is mid-flight; do NOT create a competing entry
        absent:
          accounted      = Σ residents.vram_accounted + Σ reservations.est_bytes
          effective_free = NVML.live_free() - unmaterialized    # concurrent loads deducted
          fits_budget    = accounted + est_bytes <= usable_capacity
          fits_live      = est_bytes <= effective_free - safety_headroom
          if fits_budget and fits_live:
              record reservation{op_id, model_key, est_bytes, generation, waiters: {}}
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
                  return Refuse(gpu_busy, retry_after) # 503 + Retry-After — outstanding reservations or
                                                       # an unaccounted external consumer; retryable
              mark each victim 'draining'; record evict_intent{op_id, victims}
              plan = EvictThenRetry
    # ---- stage 2: I/O strictly OUTSIDE the lock ----
    case plan:
      AwaitLoad:       await the load's DISPOSITION (bounded by deadline) — see Load waiters
                       on Grant:     the loader assigned this waiter a claim; return Share(claim)
                       on Refuse(x): return Refuse(x)            # the loader's own terminal outcome, verbatim
                       on Retry:     continue                    # consumes one attempt; re-derive both bounds
                       on deadline:  under lock:
                                       if a disposition was ALREADY assigned to this waiter:
                                           take it and handle it as above   # never abandon an assigned claim
                                       deregister op_id from reservation.waiters (if it still exists)
                                     return Refuse(gpu_busy, retry_after)
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
        waiters = reservation.waiters           # ← captured BEFORE the reservation goes away
        drop reservation                        # ← ALWAYS: an op owns its own reservation (see below)
        if generation unchanged since reservation:
            remove residents[model_key]; bump generation
        # else: a reclaimer already removed the entry and bumped — leave ITS state alone, but our
        #       reservation was still ours to drop and nothing else would have dropped it
      unload(partial child) if one was spawned  # ← OUTSIDE the lock
      dispose(waiters, Refuse(load_failed))     # every joiner gets the loader's own outcome
      return Refuse(load_failed)                # capacity is NOT left reserved

  under lock:
    barred = job_barrier or exclusive_job          # ← a job claimed the GPU while we were loading
    stale  = generation changed since reservation (model evicted meanwhile — see note below)
    others = Σ est_bytes of reservations OTHER than this op        # ← retained, not dropped
    drift  = Σ residents.vram_accounted + real_bytes + others > usable_capacity
    waiters = reservation.waiters                  # ← read in the SAME section that decides, either way
    if barred or stale or drift:
        residents[model_key].state = rolling_back  # record INTENT only
        drop reservation
        rollback = true
    else:
        residents[model_key] = resident, vram_accounted = real_bytes
        active_requests += 1 + |waiters|           # ← claims, atomic with commit
        assign a claim to each waiter              #    (registry read + assignment are one section, so a
        drop reservation                           #     waiter cannot deregister between count and assign)
  if rollback:                                  # ← unload happens OUTSIDE the lock
    unload(child); verify NVML free rose
    under lock: remove residents[model_key]; bump generation
    outcome = Refuse(gpu_busy, retry_after) if barred   # a job owns the GPU; transient
              else Retry if stale
              else Refuse(model_too_large if real_bytes > usable_capacity - safety_headroom
                          else gpu_busy)        # drift caused by contention is retryable
    dispose(waiters, outcome)                   # ← rollback owns its joiners exactly as the failure path does
    return outcome
  dispose(waiters, Grant)                       # wake the joiners that were just handed claims
  return Grant(claim)
```

**Why the rollback is split.** Unloading inside the commit critical section would hold the coordinator
lock across child lifecycle I/O — precisely the ABBA deadlock this redesign exists to remove, and the
one `admission.py`'s own comments record. The lock records the *decision*; the unload happens after
release; a final short critical section finalizes state.

**Reservation ownership — who is allowed to drop what.** A reservation is keyed by `op_id` and is
**dropped only by the operation that recorded it**, on every exit without exception. Reclaimers —
`evict()`, and any future path that takes a slot back — remove the *resident entry* and bump the
*generation*; they never touch another operation's reservation. Two reasons this asymmetry is the right
one, both of which an earlier draft got wrong by having the stale branch skip its own cleanup on the
premise that "an eviction already reclaimed the slot":

- **Nothing else would ever drop it.** `evict()` (below) removes `residents[victim]` and bumps the
  generation; it has no reservation step, and adding one would mean one operation mutating another's
  bookkeeping under a lock the owner is not holding. So a skipped drop is a permanent leak of budget in
  `accounted` and of live-free in `unmaterialized` — capacity that no later admission can ever reclaim.
- **Over-counting briefly is safe; under-counting never is.** A reservation that outlives its usefulness
  by a few milliseconds only makes admission more conservative. The reverse — releasing capacity an
  operation might still be holding — is what invariants 1 and 2 exist to prevent.

**Why `stale` is retained even though nothing currently sets it.** No path bumps the generation of a model
still in `loading` *on behalf of another operation*: `select_victims` targets `state == resident` only, and
`admit_job`'s drain likewise touches only entries in state `resident` (see below). The barrier re-check
does end a loading model's life, but that is the **owning** operation rolling itself back and bumping its
own generation — not a third party reclaiming the slot underneath it, which is what `stale` detects.

The `stale` branches are therefore **unreachable today — deliberately defensive**, kept because every
future path that reclaims a loading slot from outside (an operator force-unload, a crash-recovery sweep)
must go through generation bumping rather than inventing a second reclamation mechanism. Two consequences
worth stating so they are not assumed away: the branch is **not** exercised by any current test, and it is
**not** the same check as `barred` — `job_barrier`/`exclusive_job` are global flags with nothing tying them
to a per-model `generation`, which is exactly why closing the barrier TOCTOU required a genuinely new
condition alongside `stale`/`drift` rather than reusing one that was already there.

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
  never increments on its own afterwards. If the load does *not* commit — failure **or** rollback — the
  loader disposes of every waiter with its own outcome rather than leaving them to time out; see
  *Load waiters* below.
- **Release is mandatory and `finally`-style.** Every terminal path — success, error, client
  disconnect, deadline — releases exactly once, under lock, updating `last_used_at`.

### Load waiters (`AwaitLoad` joiners)

Single-flight coalescing means a second request for a model already `loading` does not start its own
load — it *joins* the one in flight. That makes the loader responsible for a set of requests it did not
originate, so the set has to be enumerable and every exit from it has to be defined. `reservation.waiters`
is that registry; **registration, deregistration, and claim assignment all happen under the state lock**,
which is what makes the count-then-assign at commit indivisible.

`dispose(waiters, outcome)` hands each registered waiter the loading operation's own disposition:

| Loader outcome | Waiter receives | Waiter does |
|---|---|---|
| `Grant` | a claim, assigned at commit | returns `Share(claim)` — releases it like any other claim |
| `Refuse(load_failed)` | the same terminal refusal | returns it; does **not** retry — a missing model, spawn error, or OOM recurs, and N waiters re-racing it would only burn their attempts |
| `Refuse(model_too_large)` | the same terminal refusal | returns it — the model does not fit an empty GPU; nothing to retry |
| `Refuse(gpu_busy)` (drift from contention) | `Retry` | consumes one attempt, backs off, re-enters stage 1 — contention is transient, and the waiter's own bounds may now differ |
| `Retry` (stale) | `Retry` | same |

The rule is: **a waiter adopts the loader's disposition**, except that dispositions the loader itself
treats as retryable make the waiter re-enter stage 1 rather than return — bounded by its own
`max_admission_attempts` and `deadline`, so no waiter outlives the caller contract. This resolves the
round-3 ambiguity between "on failure: continue" and "fail every waiter with the same outcome": *both*
happen, selected by which outcome the loader reached, and never left to the waiter to guess.

**Deadline is a third exit, and it is the one that could leak.** A waiter whose own deadline expires
deregisters **under lock** — but if a disposition was already assigned to it, deregistration finds that
disposition instead and the waiter takes it, claim included (and releases the claim like any other).
Without that tie-break a waiter could walk away from a claim already counted into `active_requests`,
permanently blocking every later eviction of that model: the exact invariant-4 defect the claim lifecycle
exists to prevent, reintroduced through the one path round 3 did not enumerate. Because assignment and
deregistration contend for the same lock, exactly one of them wins, and both outcomes are defined —
which is the whole reason the registry lives under the state lock rather than beside it.

Five invariants (assert-tested):

1. `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity` — bounds the accounted set.
2. Every individual load fits `live_free − unmaterialized − safety_headroom` — bounds the incoming
   load against *instantaneous* free memory, with concurrent not-yet-visible loads deducted so two
   reservations cannot both claim the same free bytes.
3. No reservation exists that is backed by a victim still resident — eviction completes before the
   replacement reservation is recorded.
4. `active_requests` equals the number of outstanding claims for that model, and is never negative.
   A model in `resident` state with `active_requests == 0` is genuinely idle and evictable.
5. Every registered `AwaitLoad` waiter reaches **exactly one** disposition — a claim, a terminal refusal,
   or a retry — and no reservation is dropped while its `waiters` set is non-empty and undisposed. No
   load-owning path (commit, rollback, failure) may exit without disposing of its joiners.

Invariants 1 and 2 are **distinct**: the first is a budget accounting bound, the second a physical
memory bound that also holds when *unaccounted external* GPU consumers are present.

## Eviction (safe — never mid-request, bounded wait)
```
evict(victim, deadline) :
  under lock: victim.state = draining        # stop admitting NEW requests to it
  wait until victim.active_requests == 0     # OUTSIDE lock, bounded by drain_timeout
      on timeout: under lock:
                    if job_barrier or exclusive_job:
                        leave victim 'draining'         # ← ownership TRANSFERS to the job's drain,
                                                        #   which holds the longer job_drain_timeout
                    else:
                        victim.state = resident         # revert; victim was NOT freed
                  return EvictFailed(busy)              # this op is done either way
  under lock: victim.state = evicting        # drained; committed to unload
  unload(victim); verify NVML free rose      # OUTSIDE lock
  under lock: remove victim; bump generation   # ← resident entry + generation ONLY, never a reservation
  return Evicted
```
Idle-first, then LRU. **Eviction never interrupts in-flight requests.** It also never drops another
operation's reservation — see *Reservation ownership* above; the generation bump is the whole of how a
reclaimer signals a displaced owner, and that owner cleans up after itself.

**Why the timeout revert is barrier-aware.** Reverting a stalled victim to `resident` is right when no job
is waiting — the victim was not freed, so it should go back to being an ordinary serving tenant. Under a
`job_barrier` it is exactly wrong: the job's marking pass has already run and does not run again, so a
victim that reverts *after* that pass is a resident nothing will ever mark, and the job can only time out.
That is the same liveness bug the *Convergence* fix closes, re-entered through a side door. Leaving it
`draining` hands it to the job's drain, which already owns every other resident and has the longer
`job_drain_timeout` budget — and if that budget also runs out, `admit_job`'s own timeout reverts every
surviving victim to `resident` in one place, so the victim is never abandoned in a transient state.

**Caller contract when blocked behind an eviction.** A request whose `EvictThenRetry` returns
`EvictFailed` does not spin: it consumes one of its `max_admission_attempts`, backs off
(exponential, jittered, capped), and re-enters stage 1 — where the victim is once again `resident`
(unless a barrier rose meanwhile, in which case this request is refused `gpu_busy` at the gate and the
victim stays with the job's drain) and may no longer be the best choice. When attempts or `deadline` are exhausted the request is
refused `gpu_busy` with `Retry-After`. **No admission path waits unbounded on another tenant's
in-flight request.**

## Jobs (exclusive)
```
admit_job(job) :
  under lock:
    if exclusive_job or job_barrier:         # ← another job owns the GPU or the transition
        return Wait(retry_after)             # exactly one owner; never overwrite a live claim
    job_barrier = true                       # ← closes the door FIRST; no new serving reservations
    mark EVERY entry in state 'resident' as 'draining'   # ← all of them, not just idle (see Convergence)
                                             #   entries in `loading` are NOT touched — their owning op
                                             #   rolls them back on the barrier (see The in-flight load)
  drain + unload (outside lock) until serving set empty, bounded by job_drain_timeout
      on timeout: under lock: revert each surviving victim to 'resident'
                              job_barrier = false; return Wait(retry_after)   # release the door
  under lock:
    assert residents empty and reservations empty      # now guaranteed, not hoped for — see below
    exclusive_job = job                                # whole GPU; blocks all co-residency
    job_barrier = false                                # exclusive_job now blocks admission
  # NEVER preempted (FR-010, FR-023a); on end: under lock exclusive_job = None
```
`job_barrier` closes serving admission **before** the drain starts. Without it, a serving reservation
granted during the drain window could materialize between "serving set empty" and setting
`exclusive_job`, so the job would start with a co-resident tenant — violating the exclusivity the
whole jobs lane depends on.

**Refusal codes on the wire.** Every `Refuse(gpu_busy)` this coordinator returns surfaces as **503 with
`Retry-After`**, and `Refuse(model_too_large)` as **413** — see
[inference-openai.md](./inference-openai.md). The `409` that `PolicyScheduler` parks on (FR-182) is the
host agent's *jobs-lane-full* contract, a different endpoint with a different meaning, and is deliberately
not reused for GPU contention.

The ownership check is what makes "at most one exclusive job" true rather than merely intended:
without it a second `admit_job` arriving against an already-empty serving set overwrites the first
job's claim, both workloads run, and whichever finishes first clears the other's ownership.

### Convergence — why marking *every* resident is the whole fix

An earlier revision marked only **idle** residents `draining`, while the exit condition was *serving set
empty*. A resident busy at that instant was never re-marked, so it stayed `resident` and the drain could
only ever reach `job_drain_timeout` — a liveness bug that made an exclusive job unstartable whenever any
model happened to be serving when the barrier rose.

The restriction was protecting nothing. `draining` means **stop admitting new requests to this model**;
it never interrupts in-flight work — `evict()` waits for `active_requests == 0` before it unloads
anything. Marking a busy resident `draining` therefore costs its in-flight requests exactly nothing.

"Every resident" means every entry in state **`resident`** — not every key in the `residents` map. An
entry in `loading` is not a resident yet and must not be marked: its load is in flight outside the lock,
and the operation that owns it is the only thing allowed to end it (which it does, on the barrier — see
the next section). Entries already `draining`/`evicting`/`rolling_back` are likewise left alone; an owning
operation is mid-flight and the drain simply waits for them, exactly as `AwaitTransient` does.

**The one way a resident can appear *after* the marking pass — and why it can't.** Skipping transient
entries is only safe if none of them can turn back into an unmarked `resident`. Exactly one path does
that: `evict()`'s `drain_timeout` reverting a stalled victim. An ordinary `admit_serving` can mark a
victim `draining` and start evicting it *before* any job exists; if the barrier then rises, that victim is
correctly skipped as transient — but its own `drain_timeout` is a separate and typically much shorter
tunable than `job_drain_timeout`, so it can expire first and revert the victim to `resident` behind the
pass that already ran. Nothing would re-mark it (the displaced caller re-enters stage 1 and is refused at
the barrier, so it does not re-evict either), and the job could only time out — the same liveness bug,
narrower. `evict()`'s revert is therefore **barrier-aware**: under a barrier the victim stays `draining`
and its ownership transfers to the job's drain. See *Why the timeout revert is barrier-aware* above.

With that path closed, the set of entries that can be in state `resident` after the marking pass is
**empty**, and the convergence argument holds without qualification. `evicting` and `rolling_back` have no
revert-to-`resident` path at all — both run to removal — so `draining` was the only one to close.

And convergence follows from the barrier itself. Stage 1 refuses **every** admission while
`job_barrier` holds — including `Share` on an already-resident model — so no resident can *gain* a
request during the drain. Every `active_requests` count is therefore monotonically decreasing, each
reaches 0 in bounded time (bounded by the longest in-flight request, not by new arrivals), and each
resident then unloads. The one-shot pass converges on its own; no release-path hook and no re-scan loop
are needed, because there is no such thing as a resident becoming *newly* eligible — they are all
eligible the moment the barrier is up.

On timeout, every surviving victim reverts to `resident`, exactly as the single-victim `evict()` path
does. A job that cannot get the GPU must leave the serving set as it found it.

### The in-flight load — stage 3's commit re-checks the barrier

A request already past stage 1 when the barrier rose is still loading, and its commit would otherwise
write a new resident *after* the drain observed an empty set. Stage 3's commit therefore adds a **third**
rollback condition alongside `stale` and `drift`:

```
  under lock:
    barred = job_barrier or exclusive_job          # ← NEW condition, not a reuse of `stale`
    stale  = generation changed since reservation
    drift  = ...
    if barred or stale or drift:
        residents[model_key].state = rolling_back
        ...
    if barred:  outcome = Refuse(gpu_busy, retry_after)   # transient — the job will finish
```

The load is unloaded and its joiners disposed with a **retryable** `gpu_busy` (invariant 5 applies to
this path exactly as to the others — a barrier rollback is contention, not failure, so waiters retry
rather than give up).

**This costs a completed model load, and that is the accepted trade.** The alternative — having the drain
also wait for `reservations` to empty — wastes nothing, but makes the job's start wait on load + service +
unload of work it never asked for, spending the `job_drain_timeout` budget on another tenant's request,
and turns the closing assert into a two-phase settle (reservations drain into residents, which then drain
again). The race is narrow: a load must have passed stage 1 in the moments before the barrier rose. For a
rare race, a barrier that means exactly one thing — **once up, nothing new becomes resident** — is worth
more than the salvaged work.

Note this is a genuinely new condition, not the `stale` branch wearing a different hat: `job_barrier` and
`exclusive_job` are global flags with nothing tying them to a per-model `generation` (see *Why `stale` is
retained* above). Both branches now exist, and they fire on different things.

**Together these two make the closing `assert` provable rather than hopeful.** `residents` empties because
every resident was marked and none can gain requests; `reservations` empties because every in-flight load
either commits before the barrier (and is then drained like any other resident) or rolls back on seeing
it. Nothing is left in flight that the assert could trip over.

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
