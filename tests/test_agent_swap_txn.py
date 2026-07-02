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

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle, swap  # noqa: E402
from test_agent_lifecycle import FakeEngine  # noqa: E402 — the shared fake adapter


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
