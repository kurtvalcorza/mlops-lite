"""018 US2 — transactional swap (T355/T357, FR-171/172, SC-108's offline half).

Offline, GPU-free. Pins the property 017 could not have: between evicting the holder and loading
the target, NO third tenant can acquire the slot — the whole evict→free→load runs under the
admission lock. Also pins the structural job guard (a `job` holder refuses preemption with no
network probe) and the wedged-holder failure path.
"""
import os
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from test_agent_lifecycle import FakeEngine  # noqa: E402 — the shared fake adapter

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle, swap  # noqa: E402


def _manager(engines):
    a = adm.Admission(vram_budget_gb=12.0,
                      gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    runtimes = {e.engine_id: lifecycle.EngineRuntime(e, a, sleep=lambda s: None)
                for e in engines}
    return lifecycle.EngineManager(a, runtimes), a


def test_swap_evicts_serving_holder_and_loads_target():
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    mgr, a = _manager([llm, vision])
    mgr.runtimes["llm"].ensure_loaded()
    res = swap.preempt_for(mgr, "vision", drain_timeout_s=1)
    assert res["swapped"] is True and res["evicted"] == "llm"
    assert a.holder()["tenant"] == "vision"
    assert not llm.spawned[0].alive           # the holder's child was actually torn down


def test_no_holder_and_self_holder_are_no_swap():
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    mgr, a = _manager([llm, vision])
    res = swap.preempt_for(mgr, "vision")     # free GPU → plain load
    assert res["swapped"] is False and a.holder()["tenant"] == "vision"
    res = swap.preempt_for(mgr, "vision")     # already the holder → no-op load
    assert res["swapped"] is False and res["evicted"] is None


def test_job_holder_is_structurally_refused():
    vision = FakeEngine("vision")
    mgr, a = _manager([vision])
    a.acquire("training", "job", est_gb=6.0)  # a running fine-tune/HPO/batch owns the slot
    try:
        swap.preempt_for(mgr, "vision")
    except swap.PreemptRefused as e:
        assert "never preempted" in str(e)
    else:
        raise AssertionError("expected PreemptRefused for a job holder")
    assert a.holder()["tenant"] == "training"  # untouched


def test_contender_can_never_acquire_between_evict_and_load():
    # The race 017's release-then-reacquire design left open (review §4.6): while a swap is in
    # flight, a contender hammers admission.acquire — it must NEVER win the slot mid-transaction;
    # the first state it can observe after the swap begins is the TARGET holding the slot.
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    mgr, a = _manager([llm, vision])
    mgr.runtimes["llm"].ensure_loaded()

    in_swap = threading.Event()
    observed = []                             # holder tenants seen by the contender's attempts
    stop = threading.Event()

    real_unload = mgr.runtimes["llm"].unload

    def slow_unload(**kw):                    # widen the evict→load window deliberately
        in_swap.set()
        result = real_unload(**kw)
        return result

    mgr.runtimes["llm"].unload = slow_unload

    def contender():
        in_swap.wait(5)
        while not stop.is_set():
            try:
                a.acquire("asr", "serving", est_gb=1.0)
                observed.append("asr-WON")    # would be the sniped-swap bug
                a.release("asr")
                return
            except adm.Held as e:
                observed.append(e.holder.get("tenant"))

    t = threading.Thread(target=contender)
    t.start()
    res = swap.preempt_for(mgr, "vision", drain_timeout_s=1)
    stop.set()
    t.join(5)
    assert res["swapped"] is True
    assert "asr-WON" not in observed, f"contender sniped the swap: {observed[:5]}"
    # every refusal the contender saw during/after the transaction names a legitimate holder
    assert set(observed) <= {"llm", "vision"}, set(observed)


def test_swap_never_deadlocks_against_a_concurrent_direct_load():
    # Internal review (018): the first design held admission.lock across the WHOLE transaction
    # while unload()/ensure_loaded() take the engine's runtime lock underneath it — but every
    # other path (a direct ensure_loaded, the reaper's idle_reap→release) takes the runtime lock
    # FIRST and the admission lock second. ABBA: a direct load of the swap TARGET while a swap
    # was mid-eviction deadlocked both threads forever. The reservation design must let both
    # finish (either order), with the target holding the slot at the end.
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    mgr, a = _manager([llm, vision])
    mgr.runtimes["llm"].ensure_loaded()

    in_swap = threading.Event()
    real_unload = mgr.runtimes["llm"].unload

    def slow_unload(**kw):                    # hold the swap mid-transaction deterministically
        in_swap.set()
        return real_unload(**kw)

    mgr.runtimes["llm"].unload = slow_unload
    results = {}

    def direct_loader():                      # the ABBA counterpart: rt.lock → admission.lock
        in_swap.wait(5)
        try:
            results["load"] = mgr.runtimes["vision"].ensure_loaded()
        except Exception as e:                # Held/EngineError are fine — deadlock is not
            results["load_err"] = type(e).__name__

    t = threading.Thread(target=direct_loader)
    t.start()
    results["swap"] = swap.preempt_for(mgr, "vision", drain_timeout_s=2)
    t.join(10)
    assert not t.is_alive(), "swap deadlocked against a concurrent direct load (ABBA)"
    assert a.holder()["tenant"] == "vision"   # the transaction still lands the target
    assert results["swap"]["evicted"] == "llm"


def test_second_swap_is_refused_while_one_is_in_flight():
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    mgr, a = _manager([llm, vision])
    a.begin_swap("vision")                    # a swap transaction is mid-flight
    try:
        try:
            swap.preempt_for(mgr, "llm")
        except swap.PreemptRefused as e:
            assert "swap" in str(e)
        else:
            raise AssertionError("expected PreemptRefused while another swap is in flight")
        # and a plain contender is refused too — the freed window belongs to the target only
        try:
            a.acquire("asr", "serving", est_gb=1.0)
        except adm.Held as e:
            assert e.holder.get("kind") == "swap-reservation"
        else:
            raise AssertionError("expected Held during a swap reservation")
    finally:
        a.end_swap("vision")
    a.acquire("asr", "serving", est_gb=1.0)   # reservation gone — admissions flow again
    a.release("asr")


def test_unavailable_target_is_refused_before_evicting_the_holder():
    # Codex round 5 (018): an unavailable/disabled/wedged TARGET used to evict a working holder
    # first and only then fail its own load — a bad swap request became an outage for the
    # resident engine. The probe must refuse up front, holder untouched.
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    vision.available_state = (False, "vision CUDA build missing — run build.sh")
    mgr, a = _manager([llm, vision])
    mgr.runtimes["llm"].ensure_loaded()
    try:
        swap.preempt_for(mgr, "vision")
    except swap.PreemptRefused as e:
        assert "unavailable" in str(e)
    else:
        raise AssertionError("expected PreemptRefused for an unavailable target")
    assert llm.spawned[0].alive               # the holder was never evicted
    assert a.holder()["tenant"] == "llm"


def test_failed_target_load_rolls_the_evicted_holder_back():
    # Codex round 5 (018): a load failure the probe can't see (spawn ok, never becomes ready)
    # happens AFTER eviction — best-effort rollback reloads the previously healthy holder
    # instead of leaving the GPU empty. Codex round 6: the reservation is RETARGETED at the
    # holder for the rollback, never dropped first — a contender arriving in that gap must
    # still be refused, or the snipe window the transaction closes would reopen.
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    vision.ready_state = False                # spawns but never readies → EngineError
    mgr, a = _manager([llm, vision])
    mgr.runtimes["llm"].ensure_loaded()

    real_reload = mgr.runtimes["llm"].ensure_loaded
    seen = {}

    def contending_reload():                  # a contender at the worst possible moment
        try:
            a.acquire("asr", "serving", est_gb=1.0)
            seen["sniped"] = True
            a.release("asr")
        except adm.Held as e:
            seen["held"] = (e.holder.get("tenant"), e.holder.get("kind"))
        return real_reload()

    mgr.runtimes["llm"].ensure_loaded = contending_reload
    try:
        swap.preempt_for(mgr, "vision", drain_timeout_s=1)
    except lifecycle.EngineError:
        pass
    else:
        raise AssertionError("expected EngineError for a never-ready target")
    assert "sniped" not in seen, "a contender acquired the slot mid-rollback"
    assert seen["held"] == ("llm", "swap-reservation")   # the window belongs to the holder
    assert a.holder()["tenant"] == "llm"      # rolled back — the GPU is not left empty
    assert len(llm.spawned) == 2 and llm.spawned[1].alive   # a fresh holder child is up
    assert mgr.runtimes["llm"].state()["state"] == "ready"
    try:                                      # and the reservation did not leak: refusals now
        a.acquire("asr", "serving", est_gb=1.0)   # name the HOLDER, not a stale swap window
    except adm.Held as e:
        assert e.holder.get("kind") != "swap-reservation"
    else:
        raise AssertionError("expected Held — llm holds the slot after rollback")



def test_second_same_target_swap_is_refused_and_reservation_not_dropped_early():
    """019/US6 (FR-194): two concurrent swaps for the SAME target must serialize. The prior guard
    only refused a *different* target, so both same-target swaps passed and shared `_swap_target`;
    the first's end_swap then dropped the reservation mid-second-transaction. Now the second begin
    is refused, and while the first swap's reservation stands a plain contender is still locked out."""
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    mgr, a = _manager([llm, vision])
    a.begin_swap("vision")                        # swap #1 for vision is mid-flight
    try:
        try:
            a.begin_swap("vision")                # swap #2, SAME target — must be refused, not admitted
        except adm.Held as e:
            assert e.holder.get("kind") == "swap-reservation"
        else:
            raise AssertionError("a second same-target swap must be refused while one is in flight")
        # the reservation still stands (swap #1 owns it) → a plain contender is locked out
        try:
            a.acquire("asr", "serving", est_gb=1.0)
        except adm.Held as e:
            assert e.holder.get("kind") == "swap-reservation"
        else:
            raise AssertionError("the reservation must still hold the slot for swap #1's target")
    finally:
        a.end_swap("vision")                      # swap #1 ends — now the reservation is truly gone
    a.acquire("asr", "serving", est_gb=1.0)       # admissions flow again
    a.release("asr")


def test_wedged_holder_fails_the_swap_loudly():
    llm, vision = FakeEngine("llm"), FakeEngine("vision")
    llm.immortal_child = True                 # eviction will fail — child survives SIGKILL
    mgr, a = _manager([llm, vision])
    mgr.runtimes["llm"].ensure_loaded()
    try:
        swap.preempt_for(mgr, "vision", drain_timeout_s=0)
    except swap.SwapError as e:
        assert "did not unload" in str(e)
    else:
        raise AssertionError("expected SwapError for a wedged holder")
    assert a.holder()["tenant"] == "llm"      # the slot still reflects reality (never freed)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
