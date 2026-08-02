"""The GPU coordinator — bounded co-residency admission (026 Phase 2, contracts/admission-scheduler.md).

Replaces the single-slot `hostagent/admission.py` lease with a **resident-set state machine**. This
is a redesign rather than an extension for one structural reason: `admission.py` is single-slot
(`_holder`, `_swap_target`) and its own comments record that holding its RLock across
evict→free→load deadlocked ABBA. Co-residency needs a *set* of residents with per-model lifecycle,
active-request ref-counts, reservations, and rollback — and it needs all of that without ever
holding the lock across child lifecycle I/O.

## The two bounds (Principle II, constitution v1.6.1)

Two **distinct** checks, and conflating them was the original design's central error:

  1. `Σ residents.vram_accounted + Σ reservations ≤ usable_capacity` — a *budget* bound.
  2. each incoming load `≤ live_free − unmaterialized − safety_headroom` — a *physical* bound.

The v1 rule ("combined VRAM ≤ live free VRAM") double-counted: live free already excludes residents,
so 6 GiB resident on a 10 GiB usable device leaves 4 GiB free, and the old constraint would call a
perfectly valid state invalid and evict for no reason. Bound 2 also holds when *unaccounted external*
GPU consumers are present, which bound 1 cannot see — which is why both exist.

## The lock discipline

`_lock` guards **state only** and is never held across `load`/`unload`. Every I/O-bearing plan is
decided under the lock and executed after releasing it. `_load_gate` serializes load+measure so a
per-PID NVML reading is attributable, and is itself never taken while `_lock` is held — the ABBA
constraint applies to it identically. `LifecycleGuard` turns that from a convention into a runtime
assertion (T635): a lifecycle call attempted while this thread holds `_lock` raises rather than
deadlocking somewhere else later.

## What a request gets back

`Share` (an already-resident model), `Grant` (a load this request drove), or `Refuse`. Both success
outcomes carry a **claim** — the ref-count that keeps a resident alive. Claims are the fix for the
round-3 defect where `Share` incremented with no defined release and a cold `Grant` never incremented
at all, so a freshly loaded model was observable as idle-and-evictable while its triggering request
was still running.
"""
import itertools
import logging
import random
import threading
import time
import uuid

from hostagent import gpuconfig

logger = logging.getLogger("hostagent.coordinator")


def _observe(outcome: str, reason: str = None) -> None:
    """Record an admission outcome (T670).

    Best-effort and never on the failure path of a decision: an observability import problem must
    not be able to refuse a request. The `reason` vocabulary is closed and names the DECIDING
    condition, so a refusal is attributable to a specific bound rather than to "the GPU was busy" —
    which is the difference between an operator raising a budget and an operator chasing a ghost.
    """
    try:
        from hostagent.metrics import REGISTRY

        REGISTRY.inc("hostagent_admission_outcomes_total", labels={"result": outcome})
        if reason:
            REGISTRY.inc("hostagent_admission_refusals_total", labels={"reason": reason})
    except Exception:  # noqa: BLE001
        pass

# -- outcomes ---------------------------------------------------------------------------------------

#: Transient — retry. Every one of these surfaces as 503 + `Retry-After` at the gateway.
GPU_BUSY = "gpu_busy"
#: Permanent — the estimate exceeds `usable_capacity − safety_headroom`, i.e. it would not fit an
#: EMPTY GPU. Surfaces as 413. Never returned for contention.
MODEL_TOO_LARGE = "model_too_large"
#: Terminal — spawn error, readiness timeout, OOM, missing model. Retrying reproduces it.
LOAD_FAILED = "load_failed"


class Outcome:
    """Base for the three admission answers, so a caller can branch on type rather than on a code."""

    ok = False


class Share(Outcome):
    """The model was already resident; this request joins it. Carries a claim."""

    ok = True

    def __init__(self, claim):
        self.claim = claim

    def __repr__(self):
        return f"Share({self.claim.model_key})"


class Grant(Outcome):
    """This request drove the load and it committed. Carries a claim."""

    ok = True

    def __init__(self, claim):
        self.claim = claim

    def __repr__(self):
        return f"Grant({self.claim.model_key})"


class Refuse(Outcome):
    """Admission refused. `code` is one of GPU_BUSY / MODEL_TOO_LARGE / LOAD_FAILED."""

    def __init__(self, code: str, message: str = "", retry_after: float = None):
        self.code = code
        self.message = message or code
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.code in (GPU_BUSY,)

    def __repr__(self):
        return f"Refuse({self.code})"


class _Retry:
    """Internal disposition: not an answer to the caller, an instruction to re-enter stage 1."""

    __slots__ = ()


RETRY = _Retry()


class EvictFailed(Exception):
    """A victim's drain did not converge within `drain_timeout`. The caller consumes an attempt."""


# -- state ------------------------------------------------------------------------------------------

LOADING = "loading"
RESIDENT = "resident"
DRAINING = "draining"
EVICTING = "evicting"
ROLLING_BACK = "rolling_back"

#: States an owning operation is mid-way through. A request for a model in one of these must AWAIT
#: that operation (T675) rather than create a competing `loading` entry the owner would then delete.
TRANSIENT = (DRAINING, EVICTING, ROLLING_BACK)


class ResidentModel:
    """One entry in the resident set. `vram_accounted_bytes` is 0 while `loading` — the reservation
    carries the estimate during that window, and counting both would double-count it."""

    __slots__ = ("model_key", "state", "vram_accounted_bytes", "active_requests", "last_used_at",
                 "child")

    def __init__(self, model_key, state=LOADING, vram_accounted_bytes=0.0, clock=time.time):
        self.model_key = model_key
        self.state = state
        self.vram_accounted_bytes = vram_accounted_bytes
        self.active_requests = 0
        self.last_used_at = clock()
        self.child = None

    def snapshot(self) -> dict:
        return {"model": self.model_key, "state": self.state,
                "vram_accounted_bytes": self.vram_accounted_bytes,
                "active_requests": self.active_requests, "last_used_at": self.last_used_at,
                "idle": self.active_requests == 0}


class Reservation:
    """A pre-authorized VRAM claim, owned by exactly one operation and dropped only by it (T687).

    `materialized` is False until the load has been reconciled to a real per-PID reading. Only
    unmaterialized reservations are deducted from live-free (bound 2): once a model's bytes are
    actually allocated they are visible in `live_free` itself, and subtracting them twice would
    refuse loads that fit.
    """

    __slots__ = ("op_id", "model_key", "est_bytes", "generation", "materialized", "waiters")

    def __init__(self, op_id, model_key, est_bytes, generation):
        self.op_id = op_id
        self.model_key = model_key
        self.est_bytes = est_bytes
        self.generation = generation
        self.materialized = False
        self.waiters = []  # ordered: joiners are woken in arrival order

    def snapshot(self) -> dict:
        return {"op_id": self.op_id, "model": self.model_key, "est_bytes": self.est_bytes,
                "materialized": self.materialized, "waiters": len(self.waiters)}


class Waiter:
    """An `AwaitLoad` joiner. Registration, deregistration, and disposition assignment all happen
    under the coordinator lock, which is what makes the commit's count-then-assign indivisible."""

    __slots__ = ("op_id", "event", "disposition")

    def __init__(self, op_id):
        self.op_id = op_id
        self.event = threading.Event()
        self.disposition = None


class Claim:
    """A held reference to a resident model. Released exactly once, on every terminal path.

    Idempotent release is deliberate: the caller's `finally` and an explicit release in the happy
    path must not double-decrement, and an unbalanced count is either a permanent eviction block or
    an eviction racing a live request.
    """

    __slots__ = ("coordinator", "model_key", "op_id", "_released", "_claim_id")

    def __init__(self, coordinator, model_key, op_id):
        self.coordinator = coordinator
        self.model_key = model_key
        self.op_id = op_id
        self._released = False
        self._claim_id = None

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.coordinator._release_claim(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# -- lifecycle + GPU seams ----------------------------------------------------------------------------

class LifecycleGuard:
    """Wraps the child lifecycle so a call made while this thread holds the coordinator lock raises.

    T635's check, as a runtime assertion rather than a lint: a lint over call graphs would have to
    prove reachability through the indirection the coordinator uses on purpose, while this fails
    loudly the first time any path — present or future — tries to load or unload under the lock.
    That is precisely the ABBA deadlock `admission.py`'s comments record, and it deserves to be
    impossible rather than merely documented.
    """

    def __init__(self, lifecycle, coordinator):
        self._lifecycle = lifecycle
        self._coordinator = coordinator

    def _check(self, op):
        if self._coordinator._holds_lock():
            raise AssertionError(
                f"lifecycle call {op!r} attempted while holding the coordinator lock — the lock "
                f"guards state only and is never held across load/unload (ABBA, admission.py:97)")

    def load(self, model_key):
        self._check("load")
        return self._lifecycle.load(model_key)

    def unload(self, model_key, child=None):
        self._check("unload")
        return self._lifecycle.unload(model_key, child)


class NullLifecycle:
    """A lifecycle that loads nothing — the default so the coordinator is constructible (and its
    state machine testable) without a GPU."""

    def load(self, model_key):
        return type("Child", (), {"pid": 0, "model_key": model_key})()

    def unload(self, model_key, child=None):
        return None


class GpuProbe:
    """Device totals, live free memory, and per-PID usage.

    **Per-PID, not a device-wide delta** (T644): two reserved models can load concurrently, so a
    pre/post free-memory delta is attributable to neither — each reading may include the other's
    allocation or catch only part of it, which double-accounts one model and *under-accounts* the
    other. The second outcome is the dangerous one: it admits later work against VRAM that is not
    actually free. Every resident is its own child process, so a per-process reading is both exact
    and naturally scoped.
    """

    def __init__(self, total_bytes=None, free_fn=None, used_by_pid_fn=None):
        self._total = total_bytes
        self._free_fn = free_fn
        self._used_fn = used_by_pid_fn

    def total_bytes(self) -> float:
        if self._total is not None:
            return float(self._total)
        return float(self._nvml(lambda h, m: m.total))

    def free_bytes(self) -> float:
        if self._free_fn is not None:
            return float(self._free_fn())
        return float(self._nvml(lambda h, m: m.free))

    def used_by_pid(self, pid: int) -> float:
        if self._used_fn is not None:
            return float(self._used_fn(pid))
        try:
            import pynvml

            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                for proc in pynvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    if proc.pid == pid and proc.usedGpuMemory:
                        return float(proc.usedGpuMemory)
            finally:
                pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    @staticmethod
    def _nvml(pick):
        try:
            import pynvml

            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                return pick(handle, pynvml.nvmlDeviceGetMemoryInfo(handle))
            finally:
                pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            return 0.0


# -- the coordinator -----------------------------------------------------------------------------------

class Coordinator:
    """The sole GPU-ordering authority for serving admission and exclusive jobs."""

    def __init__(self, config=None, gpu=None, lifecycle=None, clock=time.monotonic,
                 sleep=time.sleep, wallclock=time.time):
        self.config = config or gpuconfig.load()
        self.gpu = gpu or GpuProbe()
        self.lifecycle = LifecycleGuard(lifecycle or NullLifecycle(), self)
        self._clock = clock
        self._sleep = sleep
        self._wallclock = wallclock

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._load_gate = threading.Semaphore(1)
        self._depth = threading.local()

        self.residents = {}       # model_key -> ResidentModel
        self.reservations = {}    # op_id -> Reservation
        self.exclusive_job = None  # None | {"job_id", "started_at"}
        self.job_barrier = False
        self._generation = {}     # model_key -> monotonically increasing token

        #: Live claims, for invariant 4. Kept as ids rather than objects so a leaked Claim cannot be
        #: kept alive by this bookkeeping alone.
        self._claims = {}         # model_key -> set of claim ids
        self._claim_ids = itertools.count(1)

        #: Every disposition handed to a waiter, for invariant 5.
        self._waiter_dispositions = {}
        #: Every registered waiter that has not yet reached a disposition, so invariant 5 can name
        #: an ORPHANED one — a joiner in no reservation's registry and in no load-owning path's
        #: hands. Without this a dropped-with-joiners reservation is unobservable after the fact.
        self._live_waiters = {}      # op_id -> Waiter
        #: Waiters a load-owning path has taken responsibility for but not yet disposed of. The
        #: rollback drops its reservation under the lock and disposes AFTER the unload, so this is
        #: the window in which a joiner legitimately belongs to neither place.
        self._pending_disposal = {}  # owner op_id -> [Waiter]
        #: Operations holding eviction intent (victims marked, eviction not yet complete).
        #: Invariant 3 in its checkable form: an op may hold eviction intent OR a reservation, never
        #: both — which is exactly what "evict-then-recompute" means, and exactly what the
        #: mark-and-reserve-in-one-pass design this replaced got wrong.
        self._evict_intent = {}      # op_id -> [model_key]

    # -- lock discipline ---------------------------------------------------------------------------

    class _Locked:
        def __init__(self, coord):
            self.coord = coord

        def __enter__(self):
            self.coord._lock.acquire()
            self.coord._depth.value = getattr(self.coord._depth, "value", 0) + 1
            return self.coord

        def __exit__(self, *exc):
            self.coord._depth.value -= 1
            self.coord._lock.release()
            return False

    def _locked(self):
        return self._Locked(self)

    def _holds_lock(self) -> bool:
        return getattr(self._depth, "value", 0) > 0

    # -- derived quantities (callers must hold the lock) -------------------------------------------

    def usable_capacity(self) -> float:
        return self.config.usable_capacity(self.gpu.total_bytes())

    def _accounted(self) -> float:
        """Invariant 1's left side: every resident's accounted bytes plus EVERY reservation."""
        return (sum(r.vram_accounted_bytes for r in self.residents.values())
                + sum(res.est_bytes for res in self.reservations.values()))

    def _unmaterialized(self) -> float:
        """Invariant 2's deduction: reservations whose bytes are not yet visible in live-free."""
        return sum(res.est_bytes for res in self.reservations.values() if not res.materialized)

    def _generation_of(self, model_key) -> int:
        return self._generation.get(model_key, 0)

    def _bump_generation(self, model_key) -> int:
        self._generation[model_key] = self._generation_of(model_key) + 1
        return self._generation[model_key]

    def _reservation_for(self, model_key):
        for res in self.reservations.values():
            if res.model_key == model_key:
                return res
        return None

    # -- claims -------------------------------------------------------------------------------------

    def _grant_claim(self, model_key, op_id) -> Claim:
        """Mint a claim and increment. MUST be called under the lock, inside the same critical
        section that observed (or established) `resident` — never after it (T677)."""
        entry = self.residents[model_key]
        entry.active_requests += 1
        entry.last_used_at = self._wallclock()
        claim = Claim(self, model_key, op_id)
        claim_id = next(self._claim_ids)
        self._claims.setdefault(model_key, set()).add(claim_id)
        claim._claim_id = claim_id  # noqa: SLF001 — the coordinator owns the claim's bookkeeping
        return claim

    def _release_claim(self, claim) -> None:
        with self._locked():
            entry = self.residents.get(claim.model_key)
            if entry is not None:
                entry.active_requests = max(0, entry.active_requests - 1)
                entry.last_used_at = self._wallclock()
            ids = self._claims.get(claim.model_key)
            if ids is not None:
                ids.discard(getattr(claim, "_claim_id", None))
            self._cond.notify_all()

    # -- waiters ------------------------------------------------------------------------------------

    def _dispose(self, waiters, disposition, owner_op_id=None) -> None:
        """Hand every registered joiner the loading operation's disposition (T683).

        Called on **every** load-owning exit — commit, spawn failure, and the commit-time rollback.
        The rollback path is the one round 3 left abandoning its joiners, and `drift` is an ordinary
        concurrent-load outcome rather than a hypothetical, so a joiner was routinely left with no
        signal until its own deadline expired.
        """
        with self._locked():
            for waiter in waiters:
                if waiter.disposition is None:
                    waiter.disposition = disposition
                    self._waiter_dispositions[waiter.op_id] = disposition
                self._live_waiters.pop(waiter.op_id, None)
            if owner_op_id is not None:
                self._pending_disposal.pop(owner_op_id, None)
        for waiter in waiters:
            waiter.event.set()

    def _waiter_outcome(self, disposition):
        """Translate a loader disposition into what the waiter does.

        A waiter **adopts the loader's disposition**, except that dispositions the loader itself
        treats as retryable make the waiter re-enter stage 1 rather than return. That resolves the
        round-3 contradiction between "on failure: continue" and "fail every waiter with the same
        outcome": both happen, selected by which outcome the loader actually reached.
        """
        if isinstance(disposition, Refuse):
            if disposition.code == GPU_BUSY:
                return RETRY  # contention is transient, and this waiter's bounds may now differ
            return disposition  # load_failed / model_too_large recur — retrying only burns attempts
        return disposition

    # -- victim selection ---------------------------------------------------------------------------

    def _select_victims(self, model_key, est_bytes):
        """Idle-first, then LRU, sufficient to satisfy BOTH bounds.

        Only entries in state `resident` are eligible (T636): re-targeting one already `draining`,
        `evicting`, or `rolling_back` for another operation would race two `unload()`s against the
        same child. Returns `[]` when no eligible set satisfies both bounds — including the case
        where evicting *everything* still would not, which is how a genuinely oversized model is
        distinguished from transient contention.
        """
        eligible = [r for k, r in self.residents.items()
                    if k != model_key and r.state == RESIDENT]
        eligible.sort(key=lambda r: (r.active_requests > 0, r.last_used_at))

        capacity = self.usable_capacity()
        headroom = self.config.safety_headroom_bytes
        accounted = self._accounted()
        free = self.gpu.free_bytes()
        unmaterialized = self._unmaterialized()

        chosen, freed = [], 0.0
        for victim in eligible:
            chosen.append(victim)
            freed += victim.vram_accounted_bytes
            fits_budget = accounted - freed + est_bytes <= capacity
            fits_live = est_bytes <= (free + freed) - unmaterialized - headroom
            if fits_budget and fits_live:
                return chosen
        return []  # even evicting every eligible resident is not enough

    # -- stage 1 ---------------------------------------------------------------------------------------

    def admit_serving(self, model_key, est_bytes, *, op_id=None, deadline=None):
        """Admit a serving request, evicting and retrying within bounds. Never spins.

        Returns `Share`, `Grant`, or `Refuse`. The caller MUST release the claim on every terminal
        path — success, error, client disconnect, deadline.
        """
        op_id = op_id or f"op-{uuid.uuid4()}"
        deadline = deadline if deadline is not None else self._clock() + 60.0

        for attempt in range(1, self.config.max_admission_attempts + 1):
            if self._clock() >= deadline:
                _observe("refuse", "deadline")
                return Refuse(GPU_BUSY, "admission deadline exceeded", self._retry_after())

            plan, payload = self._stage1(model_key, est_bytes, op_id)

            if isinstance(plan, Outcome):
                return plan

            if plan == "load":
                return self._stage3(model_key, est_bytes, op_id, payload)

            if plan == "await_load":
                result = self._await_load(payload, deadline)
                if isinstance(result, Outcome):
                    return result
                self._backoff(attempt)
                continue

            if plan == "await_transient":
                self._await_transient(model_key, deadline)
                continue

            if plan == "evict_then_retry":
                try:
                    self._evict_all(payload, deadline)
                except EvictFailed:
                    pass  # consume an attempt, back off, re-derive BOTH bounds on the next pass
                finally:
                    with self._locked():
                        self._evict_intent.pop(op_id, None)
                self._backoff(attempt)
                continue

        _observe("refuse", "attempts_exhausted")
        return Refuse(GPU_BUSY, "admission attempts exhausted", self._retry_after())

    def _stage1(self, model_key, est_bytes, op_id):
        """Decide under the lock, performing NO lifecycle I/O. Returns `(plan, payload)`.

        The NVML read here is a memory query, not a lifecycle call — the constraint the lock
        discipline enforces is specifically that no child is loaded or unloaded while it is held.
        """
        with self._locked():
            if self.exclusive_job is not None or self.job_barrier:
                _observe("refuse", "exclusive_job" if self.exclusive_job else "job_barrier")
                return Refuse(GPU_BUSY, "an exclusive job holds the GPU",
                              self._retry_after()), None

            entry = self.residents.get(model_key)

            if entry is not None and entry.state == RESIDENT:
                _observe("share")
                return Share(self._grant_claim(model_key, op_id)), None

            if entry is not None and entry.state == LOADING:
                # Single-flight coalescing (T641): join the load in flight, never start a second.
                reservation = self._reservation_for(model_key)
                if reservation is not None:
                    waiter = Waiter(op_id)
                    reservation.waiters.append(waiter)
                    self._live_waiters[op_id] = waiter
                    return "await_load", waiter
                # A `loading` entry with no reservation is a torn state the owner is about to
                # finalize; treat it as transient rather than inventing a competing load.
                return "await_transient", None

            if entry is not None and entry.state in TRANSIENT:
                return "await_transient", None  # T675

            accounted = self._accounted()
            unmaterialized = self._unmaterialized()
            effective_free = self.gpu.free_bytes() - unmaterialized  # T637
            capacity = self.usable_capacity()
            headroom = self.config.safety_headroom_bytes

            fits_budget = accounted + est_bytes <= capacity
            fits_live = est_bytes <= effective_free - headroom

            if fits_budget and fits_live:
                # Invariant 3, enforced at the one place it can be violated: this op must not be
                # holding eviction intent. Stage 1 either fits as things stand OR evicts and
                # re-derives on the next attempt — it never marks a victim and reserves in the same
                # pass, which is what would record a reservation against memory not yet freed.
                assert op_id not in self._evict_intent, \
                    "invariant 3: a reservation may not be recorded while eviction intent is open"
                reservation = Reservation(op_id, model_key, est_bytes,
                                          self._generation_of(model_key))
                self.reservations[op_id] = reservation
                self.residents[model_key] = ResidentModel(model_key, LOADING,
                                                          clock=self._wallclock)
                return "load", reservation

            victims = self._select_victims(model_key, est_bytes)
            if not victims:
                # T676: distinguish PERMANENT from TRANSIENT before answering. A model that fits an
                # empty GPU but is blocked right now must get a retryable answer — telling a client
                # to give up on a request that would have succeeded seconds later is the worse error.
                if est_bytes > capacity - headroom:
                    _observe("refuse", "model_too_large")
                    return Refuse(MODEL_TOO_LARGE,
                                  f"{model_key} needs {est_bytes:.0f} bytes; usable capacity minus "
                                  f"headroom is {capacity - headroom:.0f}"), None
                # Name the bound that actually failed, not just "busy".
                _observe("refuse", "budget_bound" if not fits_budget else "live_free_bound")
                return Refuse(GPU_BUSY, "no evictable capacity right now",
                              self._retry_after()), None

            # Evict-then-recompute (T636): mark, then release the lock and complete the eviction.
            # The reservation is NOT recorded here — invariant 3 forbids a reservation backed by a
            # victim that is still resident, which is what the round-1 design did.
            for victim in victims:
                victim.state = DRAINING
            self._evict_intent[op_id] = [v.model_key for v in victims]
            self._cond.notify_all()
            _observe("evict")
            return "evict_then_retry", list(victims)

    # -- stage 2 helpers -------------------------------------------------------------------------------

    def _await_load(self, waiter, deadline):
        """Await the load's disposition, bounded by this waiter's own deadline (T684).

        The deadline exit is the one that could leak a claim: a waiter that gives up must deregister
        **under the lock**, but if the commit already assigned it a disposition, deregistration finds
        that disposition instead and the waiter takes it — claim included. Without the tie-break a
        waiter could walk away from a claim already counted into `active_requests`, permanently
        blocking every later eviction of that model.
        """
        timeout = max(0.0, deadline - self._clock())
        waiter.event.wait(timeout)

        with self._locked():
            disposition = waiter.disposition
            if disposition is None:
                # Timed out with nothing assigned: deregister so the commit cannot count us. Because
                # assignment and deregistration contend for this same lock, exactly one of them
                # wins, and both outcomes are defined — which is the whole reason the registry lives
                # under the state lock rather than beside it.
                for reservation in self.reservations.values():
                    if waiter in reservation.waiters:
                        reservation.waiters.remove(waiter)
                waiter.disposition = Refuse(GPU_BUSY, "waiter deadline exceeded",
                                            self._retry_after())
                self._waiter_dispositions[waiter.op_id] = waiter.disposition
                self._live_waiters.pop(waiter.op_id, None)
                return waiter.disposition
            # A disposition WAS already assigned — take it, claim included, and release it like any
            # other. Walking away here would abandon a claim already counted into `active_requests`,
            # permanently blocking every later eviction of that model.
            self._live_waiters.pop(waiter.op_id, None)

        outcome = self._waiter_outcome(disposition)
        return outcome if isinstance(outcome, Outcome) else None  # None -> caller retries

    def _await_transient(self, model_key, deadline) -> None:
        """Wait for the owning operation to finalize a `draining`/`evicting`/`rolling_back` entry."""
        with self._locked():
            while True:
                entry = self.residents.get(model_key)
                if entry is None or entry.state not in TRANSIENT:
                    return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return
                self._cond.wait(min(remaining, 0.25))

    def _backoff(self, attempt) -> None:
        """Exponential, jittered, capped — so N callers refused by one eviction do not re-enter
        stage 1 in lockstep and rebuild the contention that refused them."""
        self._sleep(self.config.backoff_for(attempt, jitter=random.uniform(0.5, 1.0)))

    def _retry_after(self) -> float:
        return round(min(self.config.admission_backoff_cap_s, 2.0), 3)

    # -- stage 3: load outside the lock, then commit -------------------------------------------------------

    def _stage3(self, model_key, est_bytes, op_id, reservation):
        """Load with the gate held, measure per-PID, then commit or roll back.

        Both rollback halves are split (T638): the lock records the *intent*, the `unload` happens
        after release, and a final short critical section finalizes. Unloading inside the commit
        section would hold the lock across child lifecycle I/O — exactly the ABBA deadlock this
        redesign exists to remove.
        """
        child = None
        with _Gate(self._load_gate):
            try:
                child = self.lifecycle.load(model_key)
                real_bytes = self.gpu.used_by_pid(getattr(child, "pid", 0))
            except Exception as e:  # noqa: BLE001 — spawn error, readiness timeout, OOM, missing model
                return self._load_failed(model_key, op_id, child, e)  # T674

            # Reconcile the reservation to the measured size and mark it materialized: the bytes are
            # now visible in `live_free`, so continuing to deduct them from it would double-count.
            with self._locked():
                if op_id in self.reservations:
                    self.reservations[op_id].est_bytes = real_bytes
                    self.reservations[op_id].materialized = True

        with self._locked():
            reservation = self.reservations.get(op_id)
            if reservation is None:
                # Our own reservation is gone — only we drop it, so this is a torn state rather than
                # a reclaim. Roll the load back rather than committing against bookkeeping we lost.
                rollback, waiters, outcome = True, [], Refuse(GPU_BUSY, "reservation lost",
                                                             self._retry_after())
            else:
                waiters = list(reservation.waiters)
                # T690: a job claimed the GPU while we were loading. A genuinely NEW condition, not
                # `stale` wearing a different hat — the barrier flags are global and nothing ties
                # them to a per-model generation.
                barred = self.job_barrier or self.exclusive_job is not None
                stale = self._generation_of(model_key) != reservation.generation
                # `others` is retained, not dropped (T638): checking only this load's real bytes
                # could commit a load that fits alone but breaches the budget alongside a concurrent
                # reservation.
                others = sum(r.est_bytes for r in self.reservations.values() if r.op_id != op_id)
                resident_total = sum(r.vram_accounted_bytes for k, r in self.residents.items()
                                     if k != model_key)
                drift = resident_total + real_bytes + others > self.usable_capacity()

                if barred or stale or drift:
                    entry = self.residents.get(model_key)
                    if entry is not None:
                        entry.state = ROLLING_BACK  # record INTENT only
                    del self.reservations[op_id]    # an op always drops its OWN reservation (T687)
                    # Take custody of the joiners in the SAME section that drops the reservation:
                    # they belong to no registry between here and `_dispose`, and invariant 5 must
                    # be able to tell that window apart from an actual abandonment.
                    self._pending_disposal[op_id] = list(waiters)
                    rollback = True
                    _observe("rollback",
                             "job_barrier" if barred else ("stale" if stale else "drift"))
                    if barred:
                        outcome = Refuse(GPU_BUSY, "a job claimed the GPU during this load",
                                         self._retry_after())
                    elif stale:
                        outcome = RETRY
                    else:
                        capacity = self.usable_capacity()
                        headroom = self.config.safety_headroom_bytes
                        outcome = (Refuse(MODEL_TOO_LARGE,
                                          f"{model_key} measured {real_bytes:.0f} bytes, over "
                                          f"capacity {capacity - headroom:.0f}")
                                   if real_bytes > capacity - headroom
                                   else Refuse(GPU_BUSY, "contention during load",
                                               self._retry_after()))
                else:
                    entry = self.residents[model_key]
                    entry.state = RESIDENT
                    entry.vram_accounted_bytes = real_bytes
                    entry.child = child
                    entry.last_used_at = self._wallclock()
                    # Claims for the loader AND every joiner, atomic with `state = resident` (T682).
                    # The registry read and the assignment are one critical section, so a waiter
                    # cannot deregister between the count and the assignment.
                    claim = self._grant_claim(model_key, op_id)
                    for waiter in waiters:
                        waiter_claim = self._grant_claim(model_key, waiter.op_id)
                        waiter.disposition = Share(waiter_claim)
                        self._waiter_dispositions[waiter.op_id] = waiter.disposition
                    del self.reservations[op_id]
                    rollback = False
                    outcome = Grant(claim)
                    _observe("grant")
                self._cond.notify_all()

        if rollback:
            self._rollback(model_key, child)
            # The rollback owns its joiners exactly as the failure path does. `outcome` may be
            # RETRY (the stale branch), which `_waiter_outcome` turns into a waiter retry.
            self._dispose(waiters, outcome, owner_op_id=op_id)
            return outcome if isinstance(outcome, Outcome) else Refuse(
                GPU_BUSY, "load rolled back, retry", self._retry_after())

        self._dispose(waiters, outcome, owner_op_id=op_id)  # wake the joiners just handed claims
        return outcome

    def _load_failed(self, model_key, op_id, child, error):
        """The load-failure path (T674): drop the reservation and the `loading` entry, unload any
        partial child OUTSIDE the lock, and fail every joiner with the same outcome.

        Round 3's omission here leaked capacity permanently — a failed load left its reservation and
        `loading` entry in place, so the bytes were never reclaimed and every later request for that
        model awaited a load that no longer existed.
        """
        with self._locked():
            reservation = self.reservations.pop(op_id, None)
            waiters = list(reservation.waiters) if reservation else []
            self._pending_disposal[op_id] = waiters
            entry = self.residents.get(model_key)
            if entry is not None and reservation is not None and \
                    self._generation_of(model_key) == reservation.generation:
                # Generation unchanged: the `loading` entry is still ours to remove. If it HAD
                # changed, a reclaimer already removed the entry and bumped — leave its state alone,
                # but our reservation was still ours to drop and nothing else would have dropped it.
                del self.residents[model_key]
                self._bump_generation(model_key)
            self._cond.notify_all()

        if child is not None:
            try:
                self.lifecycle.unload(model_key, child)  # OUTSIDE the lock
            except Exception:  # noqa: BLE001 — a partial child's cleanup must not mask the failure
                pass

        _observe("refuse", "load_failed")
        logger.warning("load of %s failed, reservation released: %s", model_key, error)
        outcome = Refuse(LOAD_FAILED, f"{model_key} failed to load: {error}")
        self._dispose(waiters, outcome, owner_op_id=op_id)
        return outcome

    def _rollback(self, model_key, child) -> None:
        """The unload half of the split rollback — outside the lock, then a short finalize."""
        try:
            self.lifecycle.unload(model_key, child)
        except Exception:  # noqa: BLE001
            pass
        with self._locked():
            self.residents.pop(model_key, None)
            self._bump_generation(model_key)
            self._cond.notify_all()

    # -- eviction ------------------------------------------------------------------------------------

    def evict(self, model_key, deadline=None) -> str:
        """Drain, then unload. Never interrupts an in-flight request; bounded by `drain_timeout`.

        On timeout the victim reverts to `resident` **only when no barrier is up**. Under a barrier
        it stays `draining` and ownership transfers to the job's drain: the job's one-shot marking
        pass has already run and would never re-mark a victim that reverted behind it, so reverting
        would leave a resident nothing will ever mark and the job could only time out.
        """
        deadline = deadline if deadline is not None else self._clock() + self.config.drain_timeout_s

        with self._locked():
            entry = self.residents.get(model_key)
            if entry is None:
                return "evicted"  # already gone; nothing to do
            if entry.state == RESIDENT:
                entry.state = DRAINING
            child = entry.child
            self._cond.notify_all()

        # Wait for in-flight requests to finish — OUTSIDE the lock, bounded.
        with self._locked():
            while True:
                entry = self.residents.get(model_key)
                if entry is None:
                    return "evicted"
                if entry.active_requests == 0:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    if self.job_barrier or self.exclusive_job is not None:
                        pass  # ownership TRANSFERS to the job's drain (longer budget)
                    else:
                        entry.state = RESIDENT  # revert; the victim was NOT freed
                    self._cond.notify_all()
                    raise EvictFailed(f"{model_key} still has {entry.active_requests} in flight")
                self._cond.wait(min(remaining, 0.05))
            entry.state = EVICTING  # drained; committed to unload
            child = entry.child
            self._cond.notify_all()

        try:
            self.lifecycle.unload(model_key, child)  # OUTSIDE the lock
        except Exception:  # noqa: BLE001
            pass

        with self._locked():
            # A reclaimer removes the resident entry and bumps the generation — and NEVER another
            # operation's reservation (T687). The generation bump is the whole of how it signals a
            # displaced owner, and that owner cleans up after itself.
            self.residents.pop(model_key, None)
            self._bump_generation(model_key)
            self._cond.notify_all()
        return "evicted"

    def _evict_all(self, victims, deadline) -> None:
        """Evict each victim, bounded by `drain_timeout` per victim.

        The per-victim bound is `drain_timeout`, NOT the caller's admission deadline — those are two
        different budgets and conflating them makes a single busy victim consume the requesting
        tenant's entire deadline in one attempt, so the bounded retry loop never gets a second pass
        and `max_admission_attempts` stops meaning anything. The admission deadline still caps it,
        since waiting past it could not help.
        """
        failures = []
        for victim in victims:
            drain_bound = min(self._clock() + self.config.drain_timeout_s, deadline)
            try:
                self.evict(victim.model_key, drain_bound)
            except EvictFailed as e:
                failures.append(e)
        if failures:
            raise EvictFailed("; ".join(str(f) for f in failures))

    # -- exclusive jobs ------------------------------------------------------------------------------

    def admit_job(self, job_id, deadline=None):
        """Take the whole GPU for an exclusive job. Returns True, or False to wait and retry.

        Never preempts, and never overwrites a live claim (T642): a second `admit_job` arriving
        against an already-empty serving set must WAIT, not overwrite the first job's ownership —
        otherwise both workloads run and whichever finishes first clears the other's claim.
        """
        deadline = (deadline if deadline is not None
                    else self._clock() + self.config.job_drain_timeout_s)

        with self._locked():
            if self.exclusive_job is not None or self.job_barrier:
                return False  # exactly one owner of the GPU, and of the transition to it
            self.job_barrier = True  # closes the door FIRST: no new serving reservations
            # Mark EVERY entry in state `resident` — not only the idle ones. `draining` means "stop
            # admitting new requests"; it never interrupts in-flight work, so marking a busy resident
            # costs its requests nothing. Marking only idle residents was a liveness bug: a resident
            # busy at that instant was never re-marked, so the drain could only ever time out.
            victims = [r for r in self.residents.values() if r.state == RESIDENT]
            for victim in victims:
                victim.state = DRAINING
            self._cond.notify_all()

        # Entries in `loading` are deliberately NOT marked: their load is in flight outside the lock
        # and the owning operation is the only thing allowed to end it — which it does, on the
        # barrier, in stage 3's commit re-check (T690).
        drained = self._drain_serving_set(deadline)

        with self._locked():
            if not drained or self.residents or self.reservations:
                for victim in self.residents.values():
                    if victim.state == DRAINING:
                        victim.state = RESIDENT  # leave the serving set as we found it
                self.job_barrier = False
                self._cond.notify_all()
                return False
            assert not self.residents, "the serving set must be empty before an exclusive job"
            assert not self.reservations, "no reservation may outlive the drain"
            self.exclusive_job = {"job_id": job_id, "started_at": self._wallclock()}
            self.job_barrier = False  # `exclusive_job` now blocks admission
            self._cond.notify_all()
        return True

    def _drain_serving_set(self, deadline) -> bool:
        """Drain every marked resident. Converges because the barrier refuses every admission —
        including `Share` on an already-resident model — so no resident can *gain* a request during
        the drain. Every count is monotonically decreasing and each reaches 0 in bounded time."""
        while True:
            with self._locked():
                pending = [r.model_key for r in self.residents.values()
                           if r.state in (DRAINING, RESIDENT)]
                in_flight = [r.model_key for r in self.residents.values()
                             if r.state in (EVICTING, ROLLING_BACK, LOADING)]
            if not pending and not in_flight:
                return True
            if self._clock() >= deadline:
                return False
            for model_key in pending:
                try:
                    self.evict(model_key, deadline)
                except EvictFailed:
                    pass  # the bounded wait below re-checks; the deadline is the real bound
            if in_flight and not pending:
                self._sleep(0.01)  # an owning op is finalizing; the barrier already refuses new work

    def end_job(self, job_id=None) -> None:
        with self._locked():
            if job_id is None or (self.exclusive_job or {}).get("job_id") == job_id:
                self.exclusive_job = None
            self._cond.notify_all()

    # -- invariants (T643) -----------------------------------------------------------------------------

    def check_invariants(self) -> list:
        """Return a list of violated invariants (empty when the state is sound).

        Asserted continuously in CI rather than only at admission: the failure modes these guard
        against are *interleavings*, and an assertion that only runs on the decision path cannot see
        a state a concurrent release or eviction produced between two decisions.
        """
        with self._locked():
            violations = []

            capacity = self.usable_capacity()
            accounted = self._accounted()
            if accounted > capacity + 1e-6:
                violations.append(
                    f"invariant 1: accounted {accounted:.0f} > usable_capacity {capacity:.0f}")

            # Invariant 3: no reservation is backed by a victim still resident. Checked per
            # OPERATION, which is the only form that catches the regression it guards against: an
            # op that both holds eviction intent and a reservation has reserved against memory its
            # own victims have not yet released. Checking "does any draining entry coexist with any
            # reservation" would false-positive constantly — an unrelated model can legitimately be
            # draining while another's load is in flight.
            for op_id in set(self._evict_intent) & set(self.reservations):
                violations.append(
                    f"invariant 3: op {op_id} holds a reservation AND eviction intent "
                    f"{self._evict_intent[op_id]}")

            for model_key, entry in self.residents.items():
                live = len(self._claims.get(model_key, ()))
                if entry.active_requests < 0:
                    violations.append(f"invariant 4: {model_key} active_requests is negative")
                if entry.active_requests != live:
                    violations.append(
                        f"invariant 4: {model_key} active_requests={entry.active_requests} but "
                        f"{live} claims are outstanding")

            # Invariant 5: every registered joiner reaches exactly one disposition. The checkable
            # form is ORPHANHOOD — a live waiter that is in no reservation's registry and in no
            # load-owning path's custody is one nothing will ever wake, which is precisely what a
            # reservation dropped with undisposed joiners produces.
            registered = {w.op_id for res in self.reservations.values() for w in res.waiters}
            in_custody = {w.op_id for waiters in self._pending_disposal.values() for w in waiters}
            for op_id, waiter in self._live_waiters.items():
                if waiter.disposition is None and op_id not in registered and op_id not in in_custody:
                    violations.append(
                        f"invariant 5: waiter {op_id} is orphaned — no registry, no custody, no "
                        f"disposition")
            return violations

    def assert_invariants(self) -> None:
        violations = self.check_invariants()
        assert not violations, "coordinator invariants violated: " + "; ".join(violations)

    # -- observability (T689) ----------------------------------------------------------------------------

    def refresh_metrics(self) -> None:
        """Publish the residency + VRAM gauges (T670). Best-effort, like `_observe`."""
        try:
            from hostagent.metrics import REGISTRY

            with self._locked():
                REGISTRY.set_gauge("hostagent_residents", len(self.residents))
                REGISTRY.set_gauge("hostagent_vram_accounted_bytes",
                                   sum(r.vram_accounted_bytes for r in self.residents.values()))
                REGISTRY.set_gauge("hostagent_vram_reserved_bytes",
                                   sum(r.est_bytes for r in self.reservations.values()))
                REGISTRY.set_gauge("hostagent_vram_unmaterialized_bytes", self._unmaterialized())
                REGISTRY.set_gauge("hostagent_vram_usable_capacity_bytes", self.usable_capacity())
                REGISTRY.set_gauge("hostagent_job_barrier", 1 if self.job_barrier else 0)
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict:
        """Both terms of both bounds, named as the contract names them, so an operator can assert
        invariants 1 and 2 from this response alone rather than inferring them from agent logs."""
        self.refresh_metrics()
        with self._locked():
            reservations = [r.snapshot() for r in self.reservations.values()]
            return {
                "resident": [r.snapshot() for r in self.residents.values()],
                "reservations": reservations,
                "vram": {
                    "usable_capacity": self.usable_capacity(),
                    "accounted": sum(r.vram_accounted_bytes for r in self.residents.values()),
                    "reserved": sum(r.est_bytes for r in self.reservations.values()),
                    "unmaterialized": self._unmaterialized(),
                    "live_free": self.gpu.free_bytes(),
                    "safety_headroom": self.config.safety_headroom_bytes,
                },
                "active_job": self.exclusive_job,
                "job_barrier": self.job_barrier,
            }


class _Gate:
    """`with _Gate(semaphore)` — the load gate, which is a LIFECYCLE gate and is never taken while
    the state lock is held (the ABBA constraint applies to it identically)."""

    def __init__(self, semaphore):
        self.semaphore = semaphore

    def __enter__(self):
        self.semaphore.acquire()
        return self

    def __exit__(self, *exc):
        self.semaphore.release()
        return False
