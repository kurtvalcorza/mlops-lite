"""022 T466/T481 — reload-on-select under the single-GPU lease (offline, GPU-free).

Pins the two-case switch (contracts/serving-resolution.md, spec review PR #64 §1):
cross-tenant displacement reuses the transactional swap; the SAME-TENANT model switch (llm
resident with a different model — where preempt_for is a satisfied no-op) force-reloads
(unload → ensure_loaded) so llama-server actually re-spawns with the new artifact. Also pins:
idempotent no-op for the already-resident model+version (FR-256); job holders never preempted
(FR-259); a GPU batch's llm holder never reloaded under it (FR-155); displacement demands the
operator confirm (`preempt=true`, FR-258); the target is probed BEFORE any unload so a bad
artifact never takes down a working holder (FR-257); strictly sequential — never two children
alive across the switch (SC-147).
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle, swap  # noqa: E402
from test_agent_lifecycle import FakeEngine  # noqa: E402 — the shared fake adapter


class FakeLlm(FakeEngine):
    """FakeEngine + the 022 binding surface the reload verb drives."""

    def __init__(self, est=1.0):
        super().__init__("llm", True, est)
        self.bound = ("model-a", "1")   # what the resolver currently points at
        self.loaded = None              # captured at spawn (the resident identity)
        self.rebinds = []

    def rebind(self, force=False):
        self.rebinds.append(force)

    def bound_identity(self):
        return self.bound

    def loaded_identity(self):
        return self.loaded

    def spawn(self):
        self.loaded = self.bound
        return super().spawn()


def _manager(extra=()):
    a = adm.Admission(vram_budget_gb=12.0,
                      gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    llm = FakeLlm()
    engines = [llm, *extra]
    runtimes = {e.engine_id: lifecycle.EngineRuntime(e, a, sleep=lambda s: None)
                for e in engines}
    return lifecycle.EngineManager(a, runtimes), a, llm


def test_idle_gpu_loads_the_selected_llm_immediately():
    mgr, a, llm = _manager()
    res = swap.reload_serving_llm(mgr)
    assert res["status"] == "loaded" and res["model_name"] == "model-a"
    assert a.holder()["tenant"] == "llm" and llm.loaded == ("model-a", "1")
    assert llm.rebinds == [True]  # the promote's reload always resolves FRESH (TTL busted)


def test_same_model_resident_is_an_idempotent_noop():
    mgr, a, llm = _manager()
    mgr.runtimes["llm"].ensure_loaded()
    res = swap.reload_serving_llm(mgr, preempt=True)
    assert res["status"] == "noop" and len(llm.spawned) == 1  # no gratuitous reload (FR-256)


def test_same_tenant_model_switch_force_reloads():
    # THE case preempt_for silently no-ops on (PR #64 §1): llm resident with model-a, promote
    # model-b — the child must actually re-spawn with the new artifact.
    mgr, a, llm = _manager()
    mgr.runtimes["llm"].ensure_loaded()
    first_child = llm.spawned[0]
    llm.bound = ("model-b", "2")
    res = swap.reload_serving_llm(mgr, preempt=True)
    assert res["status"] == "reloaded" and res["model_name"] == "model-b"
    assert len(llm.spawned) == 2 and llm.loaded == ("model-b", "2")
    assert not first_child.alive              # old child torn down BEFORE the new spawn
    assert a.holder()["tenant"] == "llm"


def test_switch_requires_operator_confirm_when_displacing():
    mgr, a, llm = _manager()
    mgr.runtimes["llm"].ensure_loaded()
    llm.bound = ("model-b", "2")
    with pytest.raises(swap.PreemptRefused, match="confirmation required"):
        swap.reload_serving_llm(mgr)          # no preempt=true → FR-258 refusal
    assert llm.spawned[0].alive and llm.loaded == ("model-a", "1")  # nothing displaced


def test_cross_tenant_serving_holder_swaps_with_confirm():
    vision = FakeEngine("vision")
    mgr, a, llm = _manager(extra=[vision])
    mgr.runtimes["vision"].ensure_loaded()
    with pytest.raises(swap.PreemptRefused, match="confirmation required"):
        swap.reload_serving_llm(mgr)
    res = swap.reload_serving_llm(mgr, preempt=True)
    assert res["status"] == "swapped" and res["evicted"] == "vision"
    assert a.holder()["tenant"] == "llm" and not vision.spawned[0].alive


def test_job_holder_is_never_preempted():
    mgr, a, llm = _manager()
    a.acquire("training", "job", est_gb=6.0)  # a running fine-tune owns the slot
    with pytest.raises(swap.PreemptRefused, match="never preempted"):
        swap.reload_serving_llm(mgr, preempt=True)
    assert a.holder()["tenant"] == "training"  # untouched; the switch is deferred (FR-259)


def test_batch_driven_llm_holder_is_not_reloaded_under_the_batch():
    mgr, a, llm = _manager()
    mgr.runtimes["llm"].ensure_loaded()
    llm.bound = ("model-b", "2")
    with pytest.raises(swap.PreemptRefused, match="FR-155"):
        swap.reload_serving_llm(mgr, preempt=True, batch_active_fn=lambda: True)
    assert llm.spawned[0].alive and llm.loaded == ("model-a", "1")


def test_unloadable_target_refuses_before_any_eviction():
    mgr, a, llm = _manager()
    mgr.runtimes["llm"].ensure_loaded()
    llm.bound = ("model-b", "2")
    llm.available_state = (False, "base GGUF not found")
    with pytest.raises(swap.PreemptRefused, match="not loadable"):
        swap.reload_serving_llm(mgr, preempt=True)
    assert llm.spawned[0].alive and llm.loaded == ("model-a", "1")  # holder untouched (FR-257)


def test_never_two_children_alive_across_the_switch():
    mgr, a, llm = _manager()
    mgr.runtimes["llm"].ensure_loaded()
    llm.bound = ("model-b", "2")
    alive_counts = []
    orig_spawn = llm.spawn

    def counting_spawn():
        alive_counts.append(sum(1 for c in llm.spawned if c.alive))
        return orig_spawn()

    llm.spawn = counting_spawn
    swap.reload_serving_llm(mgr, preempt=True)
    assert alive_counts == [0]  # at the moment of the new spawn, the old child was already dead


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
