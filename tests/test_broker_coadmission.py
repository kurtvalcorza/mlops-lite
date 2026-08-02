"""026 Phase 4 — engines co-resident behind the coordinator (T653, T654, T645, T657).

The US4 Independent Test in executable form: with a small model resident and budget headroom, an ASR
model also fits, becomes co-resident, and returns a transcript **without evicting the first** — and
both VRAM bounds hold throughout.

Driven through `CoordinatorAdmission`, the shim the engine runtimes actually call, rather than
through the coordinator directly: the claim under test is that *the engine layer* co-resides, and
testing the coordinator alone would prove only what `test_agent_coordinator.py` already proves.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import coordadmission  # noqa: E402
from hostagent import coordinator as co  # noqa: E402
from tests.test_agent_coordinator import make  # noqa: E402

GIB = 1024 ** 3


def _shim(**kw):
    coord, gpu, life = make(**kw)
    return coordadmission.CoordinatorAdmission(coord), coord, gpu, life


# -- T653/T654/T645: co-residency at the engine layer ------------------------------------------------

def test_an_asr_model_and_an_llm_serve_concurrently_without_evicting_each_other():
    """The US4 acceptance scenario, at the layer the engines use."""
    a, coord, gpu, life = _shim(total=12 * GIB, sizes={"llm": 5 * GIB, "asr": 1 * GIB})
    a.acquire("llm", "serving", 5.0)
    a.acquire("asr", "serving", 1.0)

    assert set(coord.residents) == {"llm", "asr"}
    assert all(r.state == co.RESIDENT for r in coord.residents.values())
    assert life.unloads == [], "neither model evicted the other"
    coord.assert_invariants()


def test_a_vision_model_joins_a_resident_llm_and_asr():
    a, coord, gpu, life = _shim(total=12 * GIB,
                                sizes={"llm": 5 * GIB, "asr": 1 * GIB, "vision": 2 * GIB})
    for engine, size in (("llm", 5.0), ("asr", 1.0), ("vision", 2.0)):
        a.acquire(engine, "serving", size)
    assert set(coord.residents) == {"llm", "asr", "vision"}
    assert life.unloads == []
    coord.assert_invariants()


def test_both_bounds_hold_throughout_a_co_resident_workload():
    """T657: the accounted set plus outstanding reservations never exceeds usable capacity, and
    every individual load fit live-free minus unmaterialized minus headroom."""
    a, coord, gpu, life = _shim(total=12 * GIB,
                                sizes={"llm": 5 * GIB, "asr": 1 * GIB, "vision": 2 * GIB,
                                       "embed": 1 * GIB})
    observed = []
    for engine, size in (("llm", 5.0), ("asr", 1.0), ("vision", 2.0), ("embed", 1.0)):
        assert gpu.free_bytes() - coord._unmaterialized() - coord.config.safety_headroom_bytes \
            >= size * GIB, f"bound 2 failed before admitting {engine}"
        a.acquire(engine, "serving", size)
        snap = coord.snapshot()
        observed.append(snap["vram"])
        assert snap["vram"]["accounted"] + snap["vram"]["reserved"] \
            <= snap["vram"]["usable_capacity"], f"bound 1 failed after admitting {engine}"
    assert len(observed) == 4
    coord.assert_invariants()


def test_a_model_that_does_not_fit_evicts_resident_serving_tenants_per_policy():
    """US4 scenario 3: a large model may consume most of the budget and force others out."""
    a, coord, gpu, life = _shim(total=12 * GIB,
                                sizes={"asr": 1 * GIB, "vision": 2 * GIB, "big": 9 * GIB})
    a.acquire("asr", "serving", 1.0)
    a.acquire("vision", "serving", 2.0)
    a.release("asr")
    a.release("vision")  # idle, so evictable

    a.acquire("big", "serving", 9.0)
    assert "big" in coord.residents
    assert life.unloads, "a resident serving tenant was evicted to fit"
    # The MINIMUM sufficient set, not everything: `select_victims` stops as soon as both bounds are
    # satisfied. Evicting more would throw away a model that still fits, and every needless eviction
    # is a cold start the next request pays for.
    assert set(life.unloads) < {"asr", "vision"}, \
        "only as many victims as both bounds required were evicted"
    assert set(coord.residents) == {"big", "vision"}
    coord.assert_invariants()


def test_the_combined_resident_footprint_never_exceeds_the_budget():
    a, coord, gpu, life = _shim(total=12 * GIB,
                                sizes={f"m{i}": 3 * GIB for i in range(6)})
    for i in range(6):
        try:
            a.acquire(f"m{i}", "serving", 3.0)
        except (adm.Held, adm.VramExceeded):
            pass
        coord.assert_invariants()
    accounted = sum(r.vram_accounted_bytes for r in coord.residents.values())
    assert accounted <= coord.usable_capacity()


# -- the legacy surface is preserved --------------------------------------------------------------------

def test_the_shim_presents_the_admission_surface_the_runtimes_call():
    """`lifecycle.py`, `swap.py`, and `jobs.py` were written against an interface and must not have
    to learn a new one — that is what makes the redesign a core change rather than a rewrite."""
    a, coord, gpu, life = _shim()
    for method in ("acquire", "release", "holder", "free_gb", "set_child",
                   "begin_swap", "end_swap", "retarget_swap"):
        assert callable(getattr(a, method)), f"missing {method}"
    assert hasattr(a, "lock")


def test_the_swap_lock_is_not_the_coordinators_own_lock():
    """Handing back the coordinator's lock would let `swap.py` hold it across evict->load — the exact
    ABBA deadlock the redesign exists to remove."""
    a, coord, gpu, life = _shim()
    assert a.lock is not coord._lock


def test_a_same_engine_reacquire_is_idempotent_and_does_not_readmit():
    """Re-running admission against your own resident model would see the low free VRAM your model
    caused — the trap the single-slot lease's same-tenant fast path avoided."""
    a, coord, gpu, life = _shim(sizes={"llm": 5 * GIB})
    first = a.acquire("llm", "serving", 5.0)
    again = a.acquire("llm", "serving", 5.0)
    assert first["tenant"] == again["tenant"] == "llm"
    assert life.loads == ["llm"], "no second load"


def test_an_oversized_model_raises_the_legacy_vram_exceeded():
    a, coord, gpu, life = _shim(total=12 * GIB)
    with pytest.raises(adm.VramExceeded):
        a.acquire("huge", "serving", 20.0)


def test_contention_raises_the_legacy_held():
    a, coord, gpu, life = _shim(total=12 * GIB, sizes={"llm": 9 * GIB}, max_attempts=1,
                                drain_timeout=0.05)
    a.acquire("llm", "serving", 9.0)  # claim retained -> busy, not evictable
    with pytest.raises(adm.Held):
        a.acquire("other", "serving", 9.0)


def test_release_is_idempotent_and_own_engine_only():
    """`acquire` no longer parks a long-lived claim, so `active_requests` is 0 from the moment the
    model is resident — which is what lets eviction ever run. Release stays idempotent and
    own-engine-only; it just has no reference count left to decrement."""
    a, coord, gpu, life = _shim(sizes={"llm": 2 * GIB})
    a.acquire("llm", "serving", 2.0)
    a.release("asr")  # not ours — a no-op
    assert coord.residents["llm"].active_requests == 0, \
        "a resident engine holds no per-request claim; residency is the resident entry"
    a.release("llm")
    a.release("llm")
    assert "llm" in coord.residents, "release means idle, not gone"
    assert coord.residents["llm"].active_requests == 0
    coord.assert_invariants()


def test_capacity_pressure_eviction_needs_no_manual_claim_release():
    """The deadlock, asserted directly.

    The shim used to hold one claim for the whole resident lifetime, so `active_requests` never
    reached 0 and `evict()` — which waits for 0 before calling the unload that would have released
    that very claim — could never complete. Every eviction test passed only because it released the
    claim by hand first, which production capacity pressure does not do.
    """
    a, coord, gpu, life = _shim(total=12 * GIB, sizes={"llm": 4 * GIB})
    a.acquire("llm", "serving", 4.0)

    assert coord.evict("llm") == "evicted", "eviction must complete without help"
    assert "llm" not in coord.residents


def test_an_exclusive_job_can_drain_a_resident_serving_engine():
    """Same deadlock, reached through the job barrier rather than capacity pressure."""
    a, coord, gpu, life = _shim(total=12 * GIB, sizes={"llm": 4 * GIB})
    a.acquire("llm", "serving", 4.0)

    assert a.acquire("train", "job", 0.0), "the job must be able to take the GPU"
    assert coord.residents == {}, "the resident was drained rather than deadlocking the barrier"


# -- jobs stay exclusive through the shim -----------------------------------------------------------------

def test_a_job_takes_the_whole_gpu_and_is_never_preempted():
    a, coord, gpu, life = _shim(sizes={"llm": 2 * GIB})
    a.acquire("llm", "serving", 2.0)
    a.release("llm")
    a.acquire("train", "job", 0.0)
    assert coord.exclusive_job["job_id"] == "train"
    assert coord.residents == {}, "the serving set is empty during a job"
    with pytest.raises(adm.Held):
        a.acquire("llm", "serving", 2.0)
    a.release("train")
    a.acquire("llm", "serving", 2.0)  # succeeds once the job ends


def test_holder_reports_the_exclusive_job_when_one_runs():
    a, coord, gpu, life = _shim()
    a.acquire("train", "job", 0.0)
    holder = a.holder()
    assert holder["tenant"] == "train" and holder["kind"] == "job"


def test_holder_reports_the_most_recently_used_resident_otherwise():
    """With co-residency there is no longer *a* holder; this is the answer that keeps the pre-broker
    consumers truthful, and `snapshot()` is the honest full one."""
    a, coord, gpu, life = _shim(sizes={"llm": 2 * GIB, "asr": 1 * GIB})
    a.acquire("llm", "serving", 2.0)
    a.acquire("asr", "serving", 1.0)
    assert a.holder()["tenant"] == "asr"
    assert {r["model"] for r in a.snapshot()["resident"]} == {"llm", "asr"}


def test_holder_is_none_on_an_empty_gpu():
    a, coord, gpu, life = _shim()
    assert a.holder() is None


# -- the phase gate ------------------------------------------------------------------------------------------

def test_co_residency_is_opt_in_and_off_by_default(monkeypatch):
    """026 ships phase-gated: co-residency changes what the GPU does under load, so it becomes the
    default only after the on-hardware drills pass. Off, the agent is byte-identical to 018."""
    monkeypatch.delenv("BROKER_COORDINATOR_ADMISSION", raising=False)
    assert coordadmission.enabled() is False

    from hostagent import main as agent_main
    assert isinstance(agent_main._build_admission(), adm.Admission)

    monkeypatch.setenv("BROKER_COORDINATOR_ADMISSION", "1")
    assert coordadmission.enabled() is True
    assert isinstance(agent_main._build_admission(), coordadmission.CoordinatorAdmission)


def test_the_engine_runtimes_and_the_broker_share_one_coordinator(monkeypatch):
    """Two coordinators would be two GPU authorities — the single thing this design forbids."""
    monkeypatch.setenv("BROKER_COORDINATOR_ADMISSION", "1")
    from hostagent import main as agent_main

    agent_main._COORDINATOR = None
    shim = agent_main._build_admission()
    coordinator, scheduler = agent_main.build_broker()
    assert shim.coordinator is coordinator
    assert scheduler.coordinator is coordinator
    agent_main._COORDINATOR = None


# -- the production wiring, over a REAL EngineRuntime ----------------------------------------------
#
# Every test above drives `CoordinatorAdmission` over the coordinator's *test* lifecycle, which is
# why they all passed while production ran on `NullLifecycle` and behaved differently. These use a
# real `hostagent.lifecycle.EngineRuntime` with a fake adapter, so the call ORDER is the real one:
#
#     acquire(...) -> adapter.spawn() -> set_child(real pid)
#
# That order is the whole problem the `RuntimeLifecycle` exists to solve: at admission time the
# process the coordinator would measure does not exist yet.

class _FakeChild:
    """What `EngineRuntime` actually drives: it calls `terminate()` on the child directly, not
    `adapter.stop()`."""

    def __init__(self, pid):
        self.pid = pid
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


class _FakeAdapter:
    """The smallest thing `EngineRuntime` will drive: spawns a child with a known PID."""

    gpu = True
    optional = False

    def __init__(self, engine_id, pid, est_gb):
        self.engine_id = engine_id
        self._pid = pid
        self._est = est_gb
        self.spawned = 0
        self.stopped = 0

    def available(self):
        return True, ""

    def estimate_vram(self):
        return self._est

    def spawn(self):
        self.spawned += 1
        return _FakeChild(self._pid)

    def ready(self):
        return True

    def stop(self, child):
        self.stopped += 1
        child.kill()


def _runtime(adapter, admission):
    from hostagent import lifecycle as lifecycle_mod
    return lifecycle_mod.EngineRuntime(adapter, admission, kind="serving", ready_wait_s=1,
                                       sleep=lambda _s: None)


def test_the_real_spawned_pid_reaches_the_coordinator(monkeypatch):
    """`set_child()` only assigned when `entry.child` was None, and the lifecycle had already put a
    placeholder there — so the real PID was silently dropped and every per-PID reading afterwards
    measured a process that does not exist."""
    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    shim = coordadmission.CoordinatorAdmission(coord)

    adapter = _FakeAdapter("llm", pid=4242, est_gb=4.0)
    runtime = _runtime(adapter, shim)
    coord.lifecycle.inner.register("llm", runtime)

    runtime.ensure_loaded()

    entry = coord.residents["llm"]
    assert entry.child is not None and entry.child.pid == 4242, \
        "the coordinator must know the PID of the process it is accounting for"


def test_the_accounted_vram_is_reconciled_against_the_real_process(monkeypatch):
    """Stage 3 cannot measure a process that does not exist yet, so the estimate stands until the
    runtime reports back — and then the REAL reading replaces it."""
    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    shim = coordadmission.CoordinatorAdmission(coord)

    adapter = _FakeAdapter("llm", pid=4242, est_gb=4.0)
    runtime = _runtime(adapter, shim)
    coord.lifecycle.inner.register("llm", runtime)

    # The probe reports the real process at 5 GiB — more than the 4 GiB estimate.
    gpu.loaded[4242] = ("llm", 5 * GIB)
    runtime.ensure_loaded()

    assert coord.residents["llm"].vram_accounted_bytes == 5 * GIB, \
        "both VRAM bounds are enforced against this number; it must be measured, not estimated"


def test_a_deferred_commit_keeps_deducting_its_bytes_from_live_free():
    """Invariant 2 must cover the window between commit and spawn.

    An earlier version of this test asserted over `coord.reservations` *after* `admit_serving()`
    returned — but `_stage3` deletes the reservation at commit on every path, so the assertion ran
    over an empty dict and passed no matter what the code did. It was vacuous, and it was hiding a
    real gap: the bytes moved to the resident and stopped being deducted from live-free entirely.

    Asserting on `_unmaterialized()` instead states the property directly, and it is the number both
    the bound and the `/admin/queue` payload actually use.
    """
    coord, _gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)

    result = coord.admit_serving("llm", 4.0 * GIB)
    assert not isinstance(result, co.Refuse)
    assert coord.reservations == {}, "the reservation is gone at commit — do not assert over it"
    assert coord.residents["llm"].materialized is False, "nothing has spawned yet"
    assert coord._unmaterialized() == 4.0 * GIB, \
        "the bytes must still be deducted from live-free until the process exists"


def test_a_second_model_cannot_be_admitted_against_vram_the_first_is_about_to_take():
    """The concrete over-admission, with an unaccounted consumer present — which is the case
    invariant 2 exists for, since invariant 1 has no visibility into it.

    12 GiB card, 3 GiB held externally, usable budget 11 GiB. Two 5 GiB models both fit the budget,
    so invariant 1 admits both; only invariant 2 can catch that 3 + 5 + 5 exceeds the card. Before
    the fix it could not see the first model's pending allocation and admitted 13 GiB onto 12.
    """
    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    gpu.external = 3 * GIB

    assert not isinstance(coord.admit_serving("A", 5.0 * GIB), co.Refuse)
    assert isinstance(coord.admit_serving("B", 5.0 * GIB), co.Refuse), \
        "B was admitted against the 5 GiB A has not allocated yet"

    committed = sum(r.vram_accounted_bytes for r in coord.residents.values())
    assert committed + gpu.external <= gpu.total_bytes(), \
        f"{(committed + gpu.external) / GIB} GiB committed on a {gpu.total_bytes() / GIB} GiB card"


def test_the_deduction_stops_once_the_real_process_reports():
    """The other half: keep deducting forever and the model is counted twice against itself, which
    refuses admissions that genuinely fit."""
    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    shim = coordadmission.CoordinatorAdmission(coord)

    coord.admit_serving("A", 5.0 * GIB)
    gpu.loaded[777] = ("A", 5 * GIB)          # the process is now real and visible in live-free
    shim.set_child("A", 777)

    assert coord.residents["A"].materialized is True
    assert coord._unmaterialized() == 0, "deducting materialized bytes double-counts the model"
    assert not isinstance(coord.admit_serving("C", 2.0 * GIB), co.Refuse)


def test_a_normally_loaded_resident_is_materialized_immediately():
    """A non-deferred load measures a live process before committing, so its bytes are already in
    live-free — deducting them again would refuse admissions that fit."""
    coord, _gpu, _life = make(total=12 * GIB, sizes={"llm": 4 * GIB})
    coord.admit_serving("llm", 4.0 * GIB)
    assert coord.residents["llm"].materialized is True
    assert coord._unmaterialized() == 0


def test_a_coordinator_eviction_unloads_the_real_engine():
    """The no-op `NullLifecycle.unload` left the real child running while the coordinator recorded
    the model as gone — the two VRAM bounds then enforced against memory still held."""
    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    shim = coordadmission.CoordinatorAdmission(coord)

    adapter = _FakeAdapter("llm", pid=4242, est_gb=4.0)
    runtime = _runtime(adapter, shim)
    coord.lifecycle.inner.register("llm", runtime)

    runtime.ensure_loaded()
    child = runtime.child
    assert child is not None, "the engine is up"

    # Drop the claim so the model is idle and therefore evictable. The engine is still LOADED — the
    # resident must survive this, which is what makes co-residency a warm cache rather than a
    # load-per-request.
    shim.release("llm")
    assert "llm" in coord.residents, "an idle model stays resident and evictable"

    coord.evict("llm")

    assert child.terminated, "eviction must stop the REAL child, not call a no-op"
    assert runtime.child is None, "and the runtime must know it is gone"
    assert "llm" not in coord.residents


def test_production_wiring_does_not_use_the_null_lifecycle(monkeypatch):
    """The finding in one assertion: `_coordinator()` built a bare `Coordinator()`, which defaults
    to a lifecycle that loads nothing and unloads nothing."""
    monkeypatch.setenv("BROKER_COORDINATOR_ADMISSION", "1")
    from hostagent import main as main_mod

    monkeypatch.setattr(main_mod, "_COORDINATOR", None)
    monkeypatch.setattr(main_mod, "_RUNTIME_LIFECYCLE", None)
    coordinator = main_mod._coordinator()

    inner = getattr(coordinator.lifecycle, "inner", coordinator.lifecycle)
    assert not isinstance(inner, co.NullLifecycle), \
        "production must not run the coordinator on a lifecycle that loads and unloads nothing"
    assert isinstance(inner, coordadmission.RuntimeLifecycle)


# -- the resident set is not leaked when the RUNTIME unloads ---------------------------------------
#
# `evict()` is coordinator-initiated and removes the entry itself. But the runtime also unloads on
# its own — an idle reap, an operator unload, a spawn that fails after admission — and in those
# cases nothing told the coordinator. `release()` decremented the claim and left the resident
# accounted, so the usable budget shrank permanently for a process that no longer existed.

def _wired(pid=1234, est_gb=4.0):
    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    shim = coordadmission.CoordinatorAdmission(coord)
    adapter = _FakeAdapter("llm", pid=pid, est_gb=est_gb)
    runtime = _runtime(adapter, shim)
    coord.lifecycle.inner.register("llm", runtime)
    return coord, gpu, shim, adapter, runtime


def test_an_ordinary_unload_frees_the_accounted_vram():
    """The compounding one: every idle reap and operator unload used to shrink the usable budget by
    that model's accounted size, for the rest of the agent's life."""
    coord, gpu, _shim, _adapter, runtime = _wired()
    gpu.loaded[1234] = ("llm", 5 * GIB)

    runtime.ensure_loaded()
    assert coord.residents["llm"].vram_accounted_bytes == 5 * GIB

    assert runtime.unload(drain_timeout_s=0)["status"] == "unloaded"
    assert "llm" not in coord.residents, \
        "the model is gone from the GPU; accounting for it starves every later admission"
    assert sum(r["vram_accounted_bytes"] for r in coord.snapshot()["resident"]) == 0


def test_a_spawn_that_fails_after_admission_leaves_nothing_accounted():
    """Admission commits a resident before the runtime spawns. If the spawn then fails, the entry
    would otherwise survive at its ESTIMATE with a child that was never real."""
    coord, _gpu, _shim, adapter, runtime = _wired()

    def boom():
        raise RuntimeError("spawn failed")

    adapter.spawn = boom
    with pytest.raises(Exception):
        runtime.ensure_loaded()

    assert "llm" not in coord.residents, "nothing loaded, so nothing may be accounted"


def test_repeated_load_and_unload_cycles_do_not_erode_the_budget():
    """The failure an operator would actually notice: admissions start refusing after enough reaps,
    with an empty GPU."""
    coord, gpu, _shim, _adapter, runtime = _wired()
    gpu.loaded[1234] = ("llm", 5 * GIB)

    for _ in range(5):
        runtime.ensure_loaded()
        runtime.unload(drain_timeout_s=0)

    assert coord.residents == {}
    # And the budget still admits a model that fits.
    assert not isinstance(coord.admit_serving("fresh", 5 * GIB), co.Refuse)


def test_an_idle_release_keeps_the_model_resident():
    """The distinction the fix turns on. A claim drop from a LOADED engine means idle — the model
    stays resident so the next request finds it warm, and dropping the claim is exactly what makes
    it evictable. Forgetting here would discard the accounting for VRAM the process still holds."""
    coord, gpu, shim, _adapter, runtime = _wired()
    gpu.loaded[1234] = ("llm", 5 * GIB)
    runtime.ensure_loaded()

    shim.release("llm")
    assert "llm" in coord.residents, "an idle model is still on the GPU"
    assert coord.residents["llm"].active_requests == 0, "and is now evictable"


def test_forget_is_a_no_op_when_the_entry_is_already_gone():
    """The eviction path removes the resident itself, then unloads through the runtime, whose
    `_teardown` calls `release()`. That second removal must be harmless."""
    coord, _gpu, _shim, _adapter, runtime = _wired()
    runtime.ensure_loaded()
    runtime.unload(drain_timeout_s=0)

    assert coord.forget("llm") is False, "already removed by the unload"
    assert coord.forget("never-existed") is False


def test_a_second_runtime_cannot_be_admitted_while_the_first_is_still_spawning():
    """The acquire→spawn window, driven concurrently through two real `EngineRuntime`s.

    This is the window the deferred-commit fix protects, exercised the way it actually occurs rather
    than by inspecting a flag: runtime A is paused *inside* `adapter.spawn()` — after admission
    committed and before `set_child()` reports the PID — while runtime B attempts its own admission.

    With an unaccounted 3 GiB consumer present, invariant 1 admits both 5 GiB models (10 <= 11 GiB
    budget) and only invariant 2 can catch that 3 + 5 + 5 exceeds a 12 GiB card. B must be refused.
    """
    import threading

    coord, gpu, _life = make(total=12 * GIB, sizes={})
    coord.lifecycle = co.LifecycleGuard(coordadmission.RuntimeLifecycle(), coord)
    shim = coordadmission.CoordinatorAdmission(coord)
    gpu.external = 3 * GIB

    spawning = threading.Event()
    release_spawn = threading.Event()

    class BlockingAdapter(_FakeAdapter):
        def spawn(self):
            spawning.set()               # admission has committed; the process does not exist yet
            release_spawn.wait(5.0)      # hold the window open
            return super().spawn()

    a_adapter = BlockingAdapter("A", pid=1111, est_gb=5.0)
    a_runtime = _runtime(a_adapter, shim)
    coord.lifecycle.inner.register("A", a_runtime)

    thread = threading.Thread(target=a_runtime.ensure_loaded, daemon=True)
    thread.start()
    assert spawning.wait(5.0), "A never reached its spawn"

    try:
        b_refused = False
        try:
            shim.acquire("B", "serving", 5.0)
        except (adm.Held, adm.VramExceeded):
            b_refused = True
        assert b_refused, (
            "B was admitted during A's spawn window, against 5 GiB A has not allocated yet — "
            f"{(sum(r.vram_accounted_bytes for r in coord.residents.values()) + gpu.external) / GIB}"
            f" GiB committed on a {gpu.total_bytes() / GIB} GiB card")
    finally:
        release_spawn.set()
        thread.join(5.0)
