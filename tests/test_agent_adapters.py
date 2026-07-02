"""018 polish — SC-114 demonstration: a new engine is ONE adapter + ONE registry row (T380).

Offline, GPU-free. The consolidation's swappability claim (constitution Principle V, FR-170):
adding a serving engine must touch a new adapter module and a registry entry — ZERO edits to
admission, lifecycle, swap, or the agent surface. This test IS the demonstration: a stub engine
defined entirely here (the "new adapter module") drives the full shared machinery — admission,
load-on-demand, idle-reap, transactional swap against another tenant, and the /engines state
listing — without modifying a single hostagent/ file.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle, swap  # noqa: E402
from test_agent_lifecycle import FakeChild, FakeEngine  # noqa: E402


class StubDiffusionEngine:
    """The hypothetical NEW engine — everything it needs, defined in one place (SC-114).
    Implements only the adapter interface from data-model.md §EngineAdapter."""

    engine_id = "diffusion"
    gpu = True
    optional = True

    def __init__(self):
        self.children = []

    def available(self):
        return (True, None)

    def estimate_vram(self):
        return 3.5

    def spawn(self):
        child = FakeChild(pid=7000 + len(self.children))
        self.children.append(child)
        return child

    def ready(self):
        return bool(self.children) and self.children[-1].alive


def _platform_with(*adapters):
    """The generic wiring any engine gets — no per-engine code beyond the adapter itself."""
    a = adm.Admission(vram_budget_gb=12.0,
                      gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    runtimes = {ad.engine_id: lifecycle.EngineRuntime(ad, a, sleep=lambda s: None)
                for ad in adapters}
    return lifecycle.EngineManager(a, runtimes), a


def test_stub_engine_runs_the_full_shared_lifecycle_unmodified():
    stub = StubDiffusionEngine()
    mgr, a = _platform_with(stub)
    rt = mgr.runtimes["diffusion"]

    assert mgr.engine_states()["diffusion"]["state"] == "cold"
    rt.ensure_loaded()                                     # admission + spawn + readiness — shared
    assert a.holder()["tenant"] == "diffusion"
    assert a.holder()["est_gb"] == 3.5                     # the adapter's estimate drove admission
    assert mgr.engine_states()["diffusion"]["state"] == "ready"

    rt.last_used = rt.clock() - 10_000                     # idle past any timeout
    assert rt.idle_reap() is True                          # the ONE shared reaper handles it
    assert a.holder() is None
    assert mgr.engine_states()["diffusion"]["state"] == "cold"


def test_stub_engine_participates_in_transactional_swap():
    stub, llm = StubDiffusionEngine(), FakeEngine("llm")
    mgr, a = _platform_with(stub, llm)
    mgr.runtimes["llm"].ensure_loaded()

    res = swap.preempt_for(mgr, "diffusion", drain_timeout_s=1)   # generic swap, new target
    assert res["swapped"] is True and res["evicted"] == "llm"
    assert a.holder()["tenant"] == "diffusion"

    res = swap.preempt_for(mgr, "llm", drain_timeout_s=1)         # and as the evictee
    assert res["evicted"] == "diffusion"
    assert not stub.children[-1].alive                            # actually torn down


def test_stub_engine_refuses_preemption_of_a_job_like_any_engine():
    stub = StubDiffusionEngine()
    mgr, a = _platform_with(stub)
    a.acquire("training", "job", est_gb=6.0)
    try:
        swap.preempt_for(mgr, "diffusion")
    except swap.PreemptRefused:
        pass
    else:
        raise AssertionError("the structural job guard must cover new engines for free")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
