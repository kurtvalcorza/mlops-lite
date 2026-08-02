"""026 Phase 2 — the GPU coordinator (T634–T645, T674–T677, T682–T684, T687, T690).

Offline and GPU-free: the coordinator takes its GPU probe and child lifecycle as seams, so every
branch of `contracts/admission-scheduler.md` is drivable deterministically. That matters more here
than anywhere else in the feature, because the defects this design exists to close are all
*interleavings* — a rollback that abandons its joiners, a waiter that walks away from an assigned
claim, a drain that can only time out. None of those reproduce reliably on hardware.

Sizes are in whole GiB throughout so the arithmetic in the assertions is readable.
"""
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from hostagent import coordinator as co  # noqa: E402
from hostagent import gpuconfig  # noqa: E402

GIB = 1024 ** 3


class FakeGpu:
    """A GPU whose free memory is whatever the loaded children account for.

    Modelling free memory as `total - Σ loaded` rather than a scripted sequence is deliberate: the
    two-bound check reads live-free at moments the test does not control, and a scripted sequence
    would silently drift out of step with the code under test.
    """

    def __init__(self, total=12 * GIB, sizes=None):
        self._total = total
        self.sizes = sizes or {}      # model_key -> real bytes it consumes once loaded
        self.loaded = {}              # pid -> (model_key, bytes)
        self.external = 0.0           # an unaccounted consumer (bound 2 must still hold)

    def total_bytes(self):
        return self._total

    def free_bytes(self):
        return self._total - sum(b for _, b in self.loaded.values()) - self.external

    def used_by_pid(self, pid):
        entry = self.loaded.get(pid)
        return entry[1] if entry else 0.0


class FakeLifecycle:
    """Children that allocate on load and free on unload, with injectable failures and delays."""

    def __init__(self, gpu, fail=(), delay=0.0):
        self.gpu = gpu
        self.fail = set(fail)
        self.delay = delay
        self.loads = []
        self.unloads = []
        self._pids = iter(range(1000, 100000))
        self.load_started = threading.Event()
        self.release_load = None  # set to an Event to hold a load open

    def load(self, model_key):
        self.loads.append(model_key)
        self.load_started.set()
        if self.delay:
            time.sleep(self.delay)
        if self.release_load is not None:
            self.release_load.wait(5.0)
        if model_key in self.fail:
            raise RuntimeError(f"{model_key} failed to spawn")
        pid = next(self._pids)
        self.gpu.loaded[pid] = (model_key, self.gpu.sizes.get(model_key, 1 * GIB))
        return type("Child", (), {"pid": pid, "model_key": model_key})()

    def unload(self, model_key, child=None):
        self.unloads.append(model_key)
        pid = getattr(child, "pid", None)
        if pid in self.gpu.loaded:
            del self.gpu.loaded[pid]
        else:
            for p, (k, _) in list(self.gpu.loaded.items()):
                if k == model_key:
                    del self.gpu.loaded[p]


def make(total=12 * GIB, sizes=None, fail=(), budget=None, **cfg):
    gpu = FakeGpu(total=total, sizes=sizes)
    lifecycle = FakeLifecycle(gpu, fail=fail)
    config = gpuconfig.CoordinatorConfig(
        safety_reserve_bytes=cfg.pop("safety_reserve", 1 * GIB),
        safety_headroom_bytes=cfg.pop("safety_headroom", 0.5 * GIB),
        max_admission_attempts=cfg.pop("max_attempts", 3),
        drain_timeout_s=cfg.pop("drain_timeout", 1.0),
        job_drain_timeout_s=cfg.pop("job_drain_timeout", 2.0),
        admission_backoff_base_s=cfg.pop("backoff_base", 0.001),
        admission_backoff_cap_s=cfg.pop("backoff_cap", 0.01),
        configured_budget_bytes=budget)
    coord = co.Coordinator(config=config, gpu=gpu, lifecycle=lifecycle)
    return coord, gpu, lifecycle


# -- T634/T635: the state machine and the lock discipline ---------------------------------------------

def test_a_cold_request_loads_and_is_granted_a_claim():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    result = coord.admit_serving("a", 2 * GIB)
    assert isinstance(result, co.Grant)
    assert coord.residents["a"].state == co.RESIDENT
    assert coord.residents["a"].vram_accounted_bytes == 2 * GIB
    assert life.loads == ["a"]
    coord.assert_invariants()


def test_a_second_request_for_a_resident_model_shares_it_without_a_second_load():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    first = coord.admit_serving("a", 2 * GIB)
    second = coord.admit_serving("a", 2 * GIB)
    assert isinstance(second, co.Share)
    assert life.loads == ["a"], "resource identity is the model, not the tenant"
    assert coord.residents["a"].active_requests == 2
    first.claim.release()
    second.claim.release()
    assert coord.residents["a"].active_requests == 0
    coord.assert_invariants()


def test_a_lifecycle_call_under_the_lock_raises_rather_than_deadlocking():
    """T635: the ABBA constraint is a runtime assertion, not a comment. A lint over call graphs
    could not prove reachability through the coordinator's deliberate indirection."""
    coord, gpu, life = make()
    with coord._locked():
        with pytest.raises(AssertionError, match="never held across load/unload"):
            coord.lifecycle.load("a")
        with pytest.raises(AssertionError):
            coord.lifecycle.unload("a")


def test_the_load_gate_is_never_taken_while_the_state_lock_is_held():
    """The gate is a lifecycle gate, so the ABBA constraint applies to it identically. Proven by
    the guard above firing on the load that the gate wraps."""
    coord, gpu, life = make(sizes={"a": 1 * GIB})
    coord.admit_serving("a", 1 * GIB)
    assert coord._load_gate._value == 1, "the gate is released on every exit"


# -- T637/T645: the two bounds, and bounded co-residency ------------------------------------------------

def test_two_small_models_serve_concurrently():
    """T645: bounded co-residency — the whole point of the redesign."""
    coord, gpu, life = make(sizes={"a": 2 * GIB, "b": 3 * GIB})
    a = coord.admit_serving("a", 2 * GIB)
    b = coord.admit_serving("b", 3 * GIB)
    assert isinstance(a, co.Grant) and isinstance(b, co.Grant)
    assert set(coord.residents) == {"a", "b"}
    assert all(r.state == co.RESIDENT for r in coord.residents.values())
    coord.assert_invariants()


def test_a_third_model_that_breaches_the_budget_evicts_an_idle_resident():
    coord, gpu, life = make(sizes={"a": 4 * GIB, "b": 4 * GIB, "c": 4 * GIB})
    coord.admit_serving("a", 4 * GIB).claim.release()
    coord.admit_serving("b", 4 * GIB).claim.release()
    result = coord.admit_serving("c", 4 * GIB)
    assert isinstance(result, co.Grant), result
    assert "c" in coord.residents
    assert life.unloads, "an idle resident was evicted to make room"
    coord.assert_invariants()


def test_the_accounted_bound_is_the_budget_not_live_free():
    """The v1 double-count: 6 GiB resident on an 11 GiB usable device leaves 5 GiB free, and a
    4 GiB load fits BOTH bounds. The old `Σ ≤ live_free` rule would have called this invalid."""
    coord, gpu, life = make(total=12 * GIB, sizes={"big": 6 * GIB, "small": 4 * GIB})
    coord.admit_serving("big", 6 * GIB).claim.release()
    assert gpu.free_bytes() == 6 * GIB
    result = coord.admit_serving("small", 4 * GIB)
    assert isinstance(result, co.Grant), "6+4 <= 11 usable, and 4 <= 6 free - 0.5 headroom"
    assert set(coord.residents) == {"big", "small"}
    coord.assert_invariants()


def test_the_live_free_bound_holds_against_an_unaccounted_external_consumer():
    """Bound 2's reason for existing: bound 1 cannot see a consumer the coordinator did not admit."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 4 * GIB})
    gpu.external = 9 * GIB  # something outside the platform took the GPU
    result = coord.admit_serving("a", 4 * GIB)
    assert isinstance(result, co.Refuse) and result.code == co.GPU_BUSY
    assert not coord.residents, "nothing was loaded against memory that is not free"


def test_concurrent_admissions_each_fitting_live_free_but_not_jointly_grant_one():
    """T637: two reservations must not both claim the same free bytes. Without deducting
    unmaterialized reservations from live-free, both would pass their own check."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 6 * GIB, "b": 6 * GIB},
                            max_attempts=1)
    life.release_load = threading.Event()  # hold both loads open so neither materializes

    results = {}

    def admit(key):
        results[key] = coord.admit_serving(key, 6 * GIB)

    threads = [threading.Thread(target=admit, args=(k,)) for k in ("a", "b")]
    [t.start() for t in threads]
    time.sleep(0.15)
    life.release_load.set()
    [t.join(10) for t in threads]

    granted = [k for k, v in results.items() if isinstance(v, co.Grant)]
    assert len(granted) == 1, f"exactly one of two 6 GiB loads may proceed: {results}"
    coord.assert_invariants()


# -- T676: transient contention vs a genuinely oversized model -------------------------------------------

def test_a_model_that_cannot_fit_an_empty_gpu_is_413():
    coord, gpu, life = make(total=12 * GIB)
    result = coord.admit_serving("huge", 20 * GIB)
    assert isinstance(result, co.Refuse) and result.code == co.MODEL_TOO_LARGE


def test_a_model_blocked_by_a_busy_resident_is_retryable_not_413():
    """T676: 413 tells a client to give up. This one would have succeeded once the other finished."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 8 * GIB, "b": 8 * GIB}, max_attempts=1,
                            drain_timeout=0.05)
    held = coord.admit_serving("a", 8 * GIB)  # claim retained -> not idle, drain will time out
    result = coord.admit_serving("b", 8 * GIB)
    assert isinstance(result, co.Refuse)
    assert result.code == co.GPU_BUSY, "contention must never surface as model_too_large"
    assert result.retry_after is not None
    held.claim.release()


def test_a_refusal_from_contention_carries_a_retry_after():
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 10 * GIB}, max_attempts=1)
    gpu.external = 8 * GIB
    result = coord.admit_serving("a", 10 * GIB)
    assert result.code == co.GPU_BUSY and result.retry_after is not None


# -- T636: evict-then-recompute, and victim eligibility ----------------------------------------------------

def test_a_reservation_is_never_recorded_while_a_victim_is_still_resident():
    """Invariant 3, in the form that catches the mark-and-reserve-in-one-pass regression."""
    coord, gpu, life = make(sizes={"a": 5 * GIB, "b": 5 * GIB, "c": 5 * GIB})
    coord.admit_serving("a", 5 * GIB).claim.release()
    coord.admit_serving("b", 5 * GIB).claim.release()

    seen = []
    original = coord._select_victims

    def spy(model_key, est):
        victims = original(model_key, est)
        seen.append(list(coord.reservations))
        return victims

    coord._select_victims = spy
    coord.admit_serving("c", 5 * GIB)
    assert all(not r for r in seen), "no reservation existed at victim-selection time"
    coord.assert_invariants()


def test_select_victims_never_targets_a_model_another_operation_is_already_evicting():
    """T636: two sequential attempts must not race two unloads against the same child."""
    coord, gpu, life = make(sizes={"a": 4 * GIB, "b": 4 * GIB})
    coord.admit_serving("a", 4 * GIB).claim.release()
    coord.residents["a"].state = co.DRAINING  # an operation already owns it
    victims = coord._select_victims("b", 4 * GIB)
    assert victims == [], "an entry in a transient state is not an eligible victim"


def test_idle_residents_are_evicted_before_busy_ones():
    coord, gpu, life = make(sizes={"idle": 4 * GIB, "busy": 4 * GIB, "new": 4 * GIB})
    coord.admit_serving("idle", 4 * GIB).claim.release()
    busy = coord.admit_serving("busy", 4 * GIB)  # claim retained
    victims = coord._select_victims("new", 4 * GIB)
    assert victims and victims[0].model_key == "idle", "idle-first, then LRU"
    busy.claim.release()


# -- T639: bounded drain, and the barrier-aware revert -------------------------------------------------------

def test_eviction_never_interrupts_an_in_flight_request():
    coord, gpu, life = make(sizes={"a": 4 * GIB}, drain_timeout=0.5)
    claim = coord.admit_serving("a", 4 * GIB).claim

    done = threading.Event()

    def evict():
        try:
            coord.evict("a")
        except co.EvictFailed:
            pass
        done.set()

    t = threading.Thread(target=evict)
    t.start()
    time.sleep(0.1)
    assert life.unloads == [], "the child must not be unloaded while a request holds a claim"
    claim.release()
    done.wait(3)
    t.join(3)
    assert life.unloads == ["a"], "once drained, the unload proceeds"


def test_a_stuck_victim_does_not_wedge_the_coordinator():
    coord, gpu, life = make(sizes={"a": 4 * GIB}, drain_timeout=0.05)
    claim = coord.admit_serving("a", 4 * GIB).claim  # never released
    with pytest.raises(co.EvictFailed):
        coord.evict("a")
    assert coord.residents["a"].state == co.RESIDENT, "a victim that was NOT freed reverts"
    claim.release()


def test_a_stalled_victim_stays_draining_under_a_barrier():
    """T639: reverting under a barrier leaves a resident the job's one-shot pass will never re-mark,
    so the job could only time out — the same liveness bug, entered through a side door."""
    coord, gpu, life = make(sizes={"a": 4 * GIB}, drain_timeout=0.05)
    claim = coord.admit_serving("a", 4 * GIB).claim
    with coord._locked():
        coord.job_barrier = True
    with pytest.raises(co.EvictFailed):
        coord.evict("a")
    assert coord.residents["a"].state == co.DRAINING, \
        "ownership transfers to the job's drain, which holds the longer budget"
    claim.release()


# -- T640: bounded attempts, never an unbounded wait ------------------------------------------------------------

def test_no_admission_path_waits_unbounded_on_another_tenants_request():
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 8 * GIB, "b": 8 * GIB},
                            max_attempts=2, drain_timeout=0.05)
    held = coord.admit_serving("a", 8 * GIB)
    started = time.monotonic()
    result = coord.admit_serving("b", 8 * GIB)
    elapsed = time.monotonic() - started
    assert isinstance(result, co.Refuse) and result.code == co.GPU_BUSY
    assert elapsed < 5.0, f"admission must be bounded, took {elapsed:.1f}s"
    held.claim.release()


def test_backoff_consumes_attempts_rather_than_spinning():
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 8 * GIB, "b": 8 * GIB},
                            max_attempts=3, drain_timeout=0.02)
    held = coord.admit_serving("a", 8 * GIB)
    attempts = []
    original = coord._stage1

    def counting(*a):
        attempts.append(1)
        return original(*a)

    coord._stage1 = counting
    coord.admit_serving("b", 8 * GIB)
    assert len(attempts) <= 3, "attempts are bounded by max_admission_attempts"
    held.claim.release()


# -- T641/T682/T683/T684: coalescing, the waiter registry, and dispositions ---------------------------------------

def test_simultaneous_first_requests_for_one_model_produce_exactly_one_load():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    life.release_load = threading.Event()
    results = {}
    barrier = threading.Barrier(6)

    def admit(i):
        barrier.wait()
        results[i] = coord.admit_serving("a", 2 * GIB)

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    life.load_started.wait(3)
    time.sleep(0.1)
    life.release_load.set()
    [t.join(10) for t in threads]

    assert life.loads == ["a"], f"N simultaneous first-requests -> exactly one load: {life.loads}"
    assert all(r.ok for r in results.values()), results
    assert coord.residents["a"].active_requests == 6, "every joiner got a claim"
    for r in results.values():
        r.claim.release()
    coord.assert_invariants()


def test_a_joiner_of_a_failed_load_gets_the_loaders_terminal_outcome_without_retrying():
    """T683: a missing model or spawn error recurs; N waiters re-racing it would only burn attempts."""
    coord, gpu, life = make(sizes={"a": 2 * GIB}, fail=("a",))
    life.release_load = threading.Event()
    results = {}
    barrier = threading.Barrier(4)

    def admit(i):
        barrier.wait()
        results[i] = coord.admit_serving("a", 2 * GIB)

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    life.load_started.wait(3)
    time.sleep(0.1)
    life.release_load.set()
    [t.join(10) for t in threads]

    assert all(isinstance(r, co.Refuse) and r.code == co.LOAD_FAILED for r in results.values()), \
        results
    assert life.loads == ["a"], "no joiner re-attempted the same failing load"
    coord.assert_invariants()


def test_a_failed_load_leaves_no_reservation_and_permits_a_fresh_attempt_later():
    """T674: round 3's omission leaked capacity permanently and stranded every later request."""
    coord, gpu, life = make(sizes={"a": 2 * GIB}, fail=("a",))
    result = coord.admit_serving("a", 2 * GIB)
    assert isinstance(result, co.Refuse) and result.code == co.LOAD_FAILED
    assert coord.reservations == {}, "capacity is not left reserved"
    assert "a" not in coord.residents, "the loading entry is gone"

    life.fail.clear()  # the model is fixed; a later request must try a FRESH load
    again = coord.admit_serving("a", 2 * GIB)
    assert isinstance(again, co.Grant)
    assert life.loads == ["a", "a"]
    coord.assert_invariants()


def test_every_joiner_of_a_rolled_back_load_is_woken_inside_the_loaders_timeframe():
    """T683's central case: the commit-time rollback is what round 3 left abandoning its joiners,
    and `drift` is an ordinary concurrent-load outcome rather than a hypothetical."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 10 * GIB})
    life.release_load = threading.Event()
    results = {}
    barrier = threading.Barrier(3)

    def admit(i):
        barrier.wait()
        # A generous per-waiter deadline: if a joiner is only woken by its OWN deadline, this test
        # would take 30s. It must be woken by the loader instead.
        results[i] = coord.admit_serving("a", 2 * GIB, deadline=time.monotonic() + 30.0)

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(3)]
    [t.start() for t in threads]
    life.load_started.wait(3)
    time.sleep(0.1)
    life.release_load.set()

    started = time.monotonic()
    [t.join(15) for t in threads]
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"joiners waited out their own deadlines ({elapsed:.1f}s)"
    assert len(results) == 3
    coord.assert_invariants()


def test_a_waiter_that_times_out_takes_an_already_assigned_claim_rather_than_abandoning_it():
    """T684: the one path round 3 did not enumerate. A waiter walking away from a claim already
    counted into `active_requests` blocks every later eviction of that model — invariant 4's leak."""
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    loader = coord.admit_serving("a", 2 * GIB)

    # Register a waiter by hand against a synthetic in-flight load, then assign it a claim and
    # expire its deadline in the same breath — the exact race the tie-break resolves.
    with coord._locked():
        reservation = co.Reservation("op-load", "a", 2 * GIB, coord._generation_of("a"))
        coord.reservations["op-load"] = reservation
        waiter = co.Waiter("op-waiter")
        reservation.waiters.append(waiter)
        coord._live_waiters["op-waiter"] = waiter
        claim = coord._grant_claim("a", "op-waiter")
        waiter.disposition = co.Share(claim)

    outcome = coord._await_load(waiter, deadline=time.monotonic() - 1)  # already expired
    assert isinstance(outcome, co.Share), "an assigned claim is taken, never abandoned"
    outcome.claim.release()

    with coord._locked():
        del coord.reservations["op-load"]
    loader.claim.release()
    assert coord.residents["a"].active_requests == 0
    coord.assert_invariants()


def test_expiring_waiter_deadlines_concurrently_with_commits_leave_no_stuck_claims():
    """T684's stress form: after the storm, nothing may be permanently un-evictable."""
    coord, gpu, life = make(sizes={"a": 1 * GIB})
    results = []

    def admit(i):
        deadline = time.monotonic() + (0.0 if i % 2 else 2.0)  # half expire immediately
        r = coord.admit_serving("a", 1 * GIB, deadline=deadline)
        results.append(r)
        if r.ok:
            r.claim.release()

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join(15) for t in threads]

    entry = coord.residents.get("a")
    if entry is not None:
        assert entry.active_requests == 0, "every claim was released"
    coord.assert_invariants()


# -- T675: the transient-state branch --------------------------------------------------------------------------

def test_a_request_arriving_mid_rollback_does_not_create_a_competing_loading_entry():
    """T675: `draining`/`evicting`/`rolling_back` matched no branch in round 2 and fell through to a
    fresh load, so a new entry was created for a model an owning operation was mid-way removing —
    and the owner's finalizer then deleted the newcomer's entry."""
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    coord.admit_serving("a", 2 * GIB).claim.release()
    with coord._locked():
        coord.residents["a"].state = co.ROLLING_BACK

    result = coord.admit_serving("a", 2 * GIB, deadline=time.monotonic() + 0.2)
    assert life.loads == ["a"], "no competing load was started for a model being removed"
    assert isinstance(result, co.Refuse)


# -- T638/T644: the split rollback, drift, and per-PID measurement -------------------------------------------------

def test_an_under_estimating_model_triggers_rollback_rather_than_a_budget_violation():
    """T644: the estimate said 2 GiB, the child actually took 10 — reconciliation catches it."""
    coord, gpu, life = make(total=12 * GIB, sizes={"liar": 10 * GIB, "a": 5 * GIB})
    coord.admit_serving("a", 5 * GIB).claim.release()
    result = coord.admit_serving("liar", 2 * GIB)  # claims 2, really needs 10; 5+10 > 11 usable
    assert isinstance(result, co.Refuse), result
    assert "liar" not in coord.residents, "the over-large child was rolled back, not committed"
    assert "liar" in life.unloads
    coord.assert_invariants()


def test_two_models_loading_concurrently_are_each_accounted_their_own_size():
    """T644: a device-wide delta is unattributable with concurrent loads — it double-accounts one
    and under-accounts the other, and the second outcome admits work against memory that is not free."""
    coord, gpu, life = make(total=24 * GIB, sizes={"a": 2 * GIB, "b": 5 * GIB},
                            safety_reserve=1 * GIB)
    results = {}

    def admit(key, size):
        results[key] = coord.admit_serving(key, size)

    threads = [threading.Thread(target=admit, args=a) for a in (("a", 2 * GIB), ("b", 5 * GIB))]
    [t.start() for t in threads]
    [t.join(10) for t in threads]

    assert coord.residents["a"].vram_accounted_bytes == 2 * GIB
    assert coord.residents["b"].vram_accounted_bytes == 5 * GIB
    for r in results.values():
        r.claim.release()
    coord.assert_invariants()


def test_the_drift_check_retains_other_outstanding_reservations():
    """T638: checking only THIS load's real bytes could commit a load that fits alone but breaches
    the budget alongside a concurrent reservation."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 6 * GIB})
    with coord._locked():
        # A concurrent operation holds 6 GiB. Ours measures 6 — fine alone, 12 > 11 together.
        other = co.Reservation("op-other", "other", 6 * GIB, 0)
        other.materialized = True
        coord.reservations["op-other"] = other

    result = coord.admit_serving("a", 1 * GIB)
    assert isinstance(result, co.Refuse), "the commit must not ignore another op's reservation"
    assert "a" not in coord.residents


def test_the_rollback_unloads_outside_the_lock():
    """The split rollback exists because unloading inside the commit section would hold the lock
    across child lifecycle I/O — the exact ABBA deadlock this redesign removes. The LifecycleGuard
    would have raised if the unload happened under the lock."""
    coord, gpu, life = make(total=12 * GIB, sizes={"liar": 10 * GIB, "a": 5 * GIB})
    coord.admit_serving("a", 5 * GIB).claim.release()
    result = coord.admit_serving("liar", 2 * GIB)
    assert isinstance(result, co.Refuse)
    assert "liar" in life.unloads, "the rollback completed rather than raising under the lock"


# -- T687: reservation ownership -------------------------------------------------------------------------------------

def test_a_load_whose_generation_was_bumped_still_releases_its_own_reserved_bytes():
    """T687: a reclaimer removes the resident entry and bumps the generation; it NEVER drops another
    operation's reservation. So a skipped drop is a permanent leak of budget and live-free."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 4 * GIB})
    before_accounted = coord._accounted()

    life.release_load = threading.Event()
    result = {}

    def admit():
        result["r"] = coord.admit_serving("a", 4 * GIB)

    t = threading.Thread(target=admit)
    t.start()
    life.load_started.wait(3)
    with coord._locked():
        coord._bump_generation("a")  # a reclaimer displaced us mid-load
    life.release_load.set()
    t.join(10)

    with coord._locked():
        assert coord.reservations == {}, "the displaced owner dropped its own reservation"
        assert coord._accounted() == before_accounted, \
            "accounted and unmaterialized returned to their pre-admission values"

    again = coord.admit_serving("a", 4 * GIB)
    assert isinstance(again, co.Grant), "the reclaimed capacity is usable by a later admission"
    again.claim.release()


def test_evict_removes_the_resident_entry_and_the_generation_only():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    coord.admit_serving("a", 2 * GIB).claim.release()
    with coord._locked():
        coord.reservations["op-other"] = co.Reservation("op-other", "b", 1 * GIB, 0)
    coord.evict("a")
    assert "op-other" in coord.reservations, "a reclaimer never touches another op's reservation"
    assert "a" not in coord.residents


# -- T677: the request-claim lifecycle -------------------------------------------------------------------------------

def test_a_freshly_loaded_model_is_never_observed_idle_while_its_request_runs():
    """T677: a cold `Grant` that reached the caller without incrementing left a window in which a
    concurrent admission would pick the model as an idle victim and unload it mid-request."""
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    result = coord.admit_serving("a", 2 * GIB)
    assert coord.residents["a"].active_requests == 1, \
        "the claim is taken atomically with state = resident, not after"
    result.claim.release()


def test_releasing_a_claim_twice_decrements_once():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    result = coord.admit_serving("a", 2 * GIB)
    result.claim.release()
    result.claim.release()
    assert coord.residents["a"].active_requests == 0
    coord.assert_invariants()


def test_a_claim_works_as_a_context_manager():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    with coord.admit_serving("a", 2 * GIB).claim:
        assert coord.residents["a"].active_requests == 1
    assert coord.residents["a"].active_requests == 0


# -- T642/T690: exclusive jobs, the barrier, and the in-flight load ------------------------------------------------------

def test_a_job_acquires_the_gpu_once_the_serving_set_drains():
    coord, gpu, life = make(sizes={"a": 4 * GIB})
    coord.admit_serving("a", 4 * GIB).claim.release()
    assert coord.admit_job("job-1") is True
    assert coord.residents == {} and coord.exclusive_job["job_id"] == "job-1"
    assert coord.job_barrier is False, "exclusive_job blocks admission once it is set"


def test_a_job_submitted_while_a_model_is_actively_serving_still_acquires_the_gpu():
    """T642's convergence fix: marking only IDLE residents made the drain unable to finish whenever
    any model happened to be serving when the barrier rose — the job could only ever time out."""
    coord, gpu, life = make(sizes={"a": 4 * GIB}, job_drain_timeout=3.0)
    claim = coord.admit_serving("a", 4 * GIB).claim  # actively serving

    outcome = {}

    def start_job():
        outcome["ok"] = coord.admit_job("job-1")

    t = threading.Thread(target=start_job)
    t.start()
    time.sleep(0.2)
    assert coord.residents["a"].state == co.DRAINING, "every resident is marked, not just idle ones"
    claim.release()
    t.join(10)
    assert outcome["ok"] is True, "the drain converged once the in-flight request finished"
    assert coord.exclusive_job is not None


def test_a_serving_admission_racing_a_job_start_cannot_become_co_resident_with_it():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    assert coord.admit_job("job-1") is True
    result = coord.admit_serving("a", 2 * GIB)
    assert isinstance(result, co.Refuse) and result.code == co.GPU_BUSY
    assert coord.residents == {}, "no model may become resident during an exclusive job"


def test_of_two_jobs_against_an_empty_serving_set_exactly_one_runs():
    """T642: without the ownership check the second `admit_job` overwrites the first's claim, both
    workloads run, and whichever finishes first clears the other's ownership."""
    coord, gpu, life = make()
    assert coord.admit_job("job-1") is True
    assert coord.admit_job("job-2") is False, "a live claim is never overwritten"
    assert coord.exclusive_job["job_id"] == "job-1"


def test_a_job_is_never_preempted_by_a_later_serving_request_or_job():
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    coord.admit_job("job-1")
    for _ in range(3):
        assert isinstance(coord.admit_serving("a", 2 * GIB), co.Refuse)
    assert coord.exclusive_job["job_id"] == "job-1"
    coord.end_job("job-1")
    assert isinstance(coord.admit_serving("a", 2 * GIB), co.Grant)


def test_a_load_in_flight_when_the_barrier_rises_never_becomes_resident():
    """T690: the commit re-checks the barrier. Without it, a load already past stage 1 would write a
    new resident AFTER the drain observed an empty set, tripping the closing assert."""
    coord, gpu, life = make(sizes={"a": 2 * GIB}, job_drain_timeout=3.0)
    life.release_load = threading.Event()
    result = {}

    def admit():
        result["r"] = coord.admit_serving("a", 2 * GIB)

    t = threading.Thread(target=admit)
    t.start()
    life.load_started.wait(3)

    with coord._locked():
        coord.job_barrier = True  # the barrier rises while the load is in flight

    life.release_load.set()
    t.join(10)

    assert isinstance(result["r"], co.Refuse), result["r"]
    assert result["r"].code == co.GPU_BUSY, "a barrier rollback is contention, so it is retryable"
    assert "a" not in coord.residents, "the in-flight load did not become resident behind the drain"
    assert "a" in life.unloads

    with coord._locked():
        coord.job_barrier = False
    retried = coord.admit_serving("a", 2 * GIB)
    assert isinstance(retried, co.Grant), "the refused request succeeds once the job is done"
    retried.claim.release()


def test_the_closing_assert_holds_under_a_racing_load():
    coord, gpu, life = make(sizes={"a": 2 * GIB}, job_drain_timeout=3.0)
    life.release_load = threading.Event()
    threading.Thread(target=lambda: coord.admit_serving("a", 2 * GIB)).start()
    life.load_started.wait(3)

    outcome = {}

    def start_job():
        outcome["ok"] = coord.admit_job("job-1")

    t = threading.Thread(target=start_job)
    t.start()
    time.sleep(0.1)
    life.release_load.set()
    t.join(10)
    if outcome.get("ok"):
        assert coord.residents == {} and coord.reservations == {}


def test_a_job_that_cannot_drain_leaves_the_serving_set_as_it_found_it():
    coord, gpu, life = make(sizes={"a": 4 * GIB}, job_drain_timeout=0.2, drain_timeout=0.1)
    claim = coord.admit_serving("a", 4 * GIB).claim  # never released
    assert coord.admit_job("job-1") is False
    assert coord.residents["a"].state == co.RESIDENT, "every surviving victim reverts"
    assert coord.job_barrier is False, "the door is released"
    claim.release()


# -- T643: the five invariants under a randomized concurrent workload -------------------------------------------------

def test_invariants_hold_under_a_randomized_concurrent_workload():
    """T643: asserted continuously, not only at admission. The failure modes these guard against are
    interleavings, and a check that only runs on the decision path cannot see a state a concurrent
    release or eviction produced between two decisions."""
    import random

    rng = random.Random(20260802)
    models = {f"m{i}": (i + 1) * GIB for i in range(5)}
    coord, gpu, life = make(total=24 * GIB, sizes=models, drain_timeout=0.2,
                            job_drain_timeout=0.5, max_attempts=2)

    stop = threading.Event()
    violations = []

    def watchdog():
        while not stop.is_set():
            found = coord.check_invariants()
            if found:
                violations.extend(found)
            time.sleep(0.005)

    def worker(seed):
        r = random.Random(seed)
        for _ in range(25):
            action = r.choices(["serve", "job", "evict"], weights=[8, 1, 2])[0]
            if action == "serve":
                key = r.choice(list(models))
                result = coord.admit_serving(key, models[key],
                                             deadline=time.monotonic() + 1.0)
                if result.ok:
                    time.sleep(r.uniform(0, 0.01))
                    result.claim.release()
            elif action == "job":
                if coord.admit_job(f"job-{seed}"):
                    time.sleep(r.uniform(0, 0.02))
                    coord.end_job(f"job-{seed}")
            else:
                try:
                    coord.evict(r.choice(list(models)), time.monotonic() + 0.2)
                except co.EvictFailed:
                    pass

    watch = threading.Thread(target=watchdog, daemon=True)
    watch.start()
    threads = [threading.Thread(target=worker, args=(s,)) for s in range(6)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    stop.set()
    watch.join(5)

    assert not violations, f"invariant violations observed: {sorted(set(violations))[:10]}"
    coord.assert_invariants()


def test_the_invariant_checker_detects_a_deliberately_corrupted_state():
    """A checker that cannot fail proves nothing — this pins that each of the checks actually fires."""
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    result = coord.admit_serving("a", 2 * GIB)

    with coord._locked():
        coord.residents["a"].vram_accounted_bytes = 999 * GIB
    assert any("invariant 1" in v for v in coord.check_invariants())
    with coord._locked():
        coord.residents["a"].vram_accounted_bytes = 2 * GIB

    with coord._locked():
        coord.residents["a"].active_requests = 7
    assert any("invariant 4" in v for v in coord.check_invariants())
    with coord._locked():
        coord.residents["a"].active_requests = 1

    with coord._locked():
        coord._evict_intent["op-x"] = ["b"]
        coord.reservations["op-x"] = co.Reservation("op-x", "b", 1 * GIB, 0)
    assert any("invariant 3" in v for v in coord.check_invariants())
    with coord._locked():
        del coord._evict_intent["op-x"], coord.reservations["op-x"]

    with coord._locked():
        orphan = co.Waiter("op-orphan")
        coord._live_waiters["op-orphan"] = orphan  # in no registry, in no custody
    assert any("invariant 5" in v for v in coord.check_invariants())
    with coord._locked():
        del coord._live_waiters["op-orphan"]

    result.claim.release()
    coord.assert_invariants()


# -- T689: the observability surface ---------------------------------------------------------------------------------

def test_the_snapshot_exposes_both_terms_of_both_bounds():
    """Drill 3 asserts invariants 1 and 2 by reading this response alone, with no recourse to logs."""
    coord, gpu, life = make(total=12 * GIB, sizes={"a": 4 * GIB})
    claim = coord.admit_serving("a", 4 * GIB).claim
    snap = coord.snapshot()

    vram = snap["vram"]
    for term in ("usable_capacity", "accounted", "reserved", "unmaterialized", "live_free",
                 "safety_headroom"):
        assert term in vram, f"{term} is needed to check a bound without doing arithmetic"
    assert vram["accounted"] + vram["reserved"] <= vram["usable_capacity"], "invariant 1"
    assert snap["resident"][0]["state"] == co.RESIDENT
    assert snap["resident"][0]["active_requests"] == 1
    assert snap["job_barrier"] is False and snap["active_job"] is None
    claim.release()


def test_a_loading_resident_is_distinguishable_from_a_settled_one():
    """Without `state`, a mid-transition observation looks like a violated invariant."""
    coord, gpu, life = make(sizes={"a": 2 * GIB})
    life.release_load = threading.Event()
    threading.Thread(target=lambda: coord.admit_serving("a", 2 * GIB)).start()
    life.load_started.wait(3)
    snap = coord.snapshot()
    assert snap["resident"][0]["state"] == co.LOADING
    assert snap["reservations"] and snap["reservations"][0]["materialized"] is False
    life.release_load.set()
