"""018 US2 — the shared tenant lifecycle with a fake engine (T354/T357, FR-167/170).

Offline, GPU-free: a fake adapter + fake child drive `hostagent.lifecycle.EngineRuntime`.
Pins: load-on-demand acquires admission then spawns; `unavailable(reason)` surfaces (R7);
reap-before-relaunch is structural for every engine (the fix llama lacked, FR-167); a child that
survives SIGKILL leaves the engine `wedged` WITH the admission slot still held (spec edge case);
idle-reap unloads and frees the slot.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle  # noqa: E402


class FakeChild:
    def __init__(self, pid=4242, immortal=False):
        self.pid = pid
        self.immortal = immortal      # survives SIGKILL (uninterruptible D-state)
        self.alive = True
        self.signals = []

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.signals.append("term")
        if not self.immortal:
            self.alive = False

    def kill(self):
        self.signals.append("kill")
        if not self.immortal:
            self.alive = False

    def wait(self, timeout=None):
        if self.alive:
            raise TimeoutError()
        return 0


class FakeEngine:
    """The adapter interface (data-model.md §EngineAdapter) with scriptable behavior."""

    def __init__(self, engine_id="fake", gpu=True, est=1.0):
        self.engine_id, self.gpu, self.optional = engine_id, gpu, False
        self.est = est
        self.available_state = (True, None)
        self.ready_state = True
        self.immortal_child = False
        self.spawned = []

    def available(self):
        return self.available_state

    def estimate_vram(self):
        return self.est

    def spawn(self):
        child = FakeChild(pid=1000 + len(self.spawned), immortal=self.immortal_child)
        self.spawned.append(child)
        return child

    def ready(self):
        return self.ready_state


def _rt(engine=None, admission=None, **kw):
    admission = admission or adm.Admission(
        vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    engine = engine or FakeEngine()
    kw.setdefault("sleep", lambda s: None)
    return lifecycle.EngineRuntime(engine, admission, **kw), engine, admission


def test_cold_load_acquires_admission_then_spawns_and_readies():
    rt, eng, a = _rt()
    ms = rt.ensure_loaded()
    assert ms >= 0 and len(eng.spawned) == 1
    assert a.holder()["tenant"] == "fake" and a.holder()["child_pid"] == eng.spawned[0].pid
    assert rt.state()["state"] == "ready"
    assert rt.ensure_loaded() == 0.0 and len(eng.spawned) == 1  # warm path: no respawn


def test_unavailable_engine_surfaces_reason_and_never_admits():
    rt, eng, a = _rt()
    eng.available_state = (False, "whisper CUDA build missing — run build.sh")
    assert rt.state() == {"state": "unavailable", "reason": eng.available_state[1]}
    try:
        rt.ensure_loaded()
    except lifecycle.EngineError as e:
        assert "unavailable" in str(e)
    else:
        raise AssertionError("expected EngineError for an unavailable engine")
    assert a.holder() is None


def test_stuck_child_is_reaped_before_relaunch_for_every_engine():
    rt, eng, a = _rt()
    rt.ensure_loaded()
    eng.ready_state = False                   # child alive but unresponsive — the stuck case
    assert rt.state()["state"] == "loading"
    eng2_ready = {"n": 0}

    def ready_after_respawn():
        # not-ready until a SECOND child exists (the reaped relaunch), then ready
        return len(eng.spawned) >= 2

    eng.ready = ready_after_respawn
    rt.ensure_loaded()
    assert len(eng.spawned) == 2              # old child reaped, fresh child spawned
    assert not eng.spawned[0].alive           # the stuck child was actually killed
    assert a.holder()["child_pid"] == eng.spawned[1].pid


def test_kill_surviving_child_wedges_and_keeps_the_slot():
    rt, eng, a = _rt()
    eng.immortal_child = True
    rt.ensure_loaded()
    res = rt.unload(drain_timeout_s=0)
    assert res["status"] == "busy" and "SIGKILL" in res["detail"]
    assert rt.state()["state"] == "wedged"
    assert a.holder() is not None             # the GPU is NOT actually free — never lie about VRAM
    try:
        rt.ensure_loaded()                    # wedged engines refuse new work loudly
    except lifecycle.EngineError as e:
        assert "wedged" in str(e)
    else:
        raise AssertionError("expected EngineError while wedged")


def test_idle_reap_unloads_and_frees_the_slot():
    clock = {"t": 0.0}
    rt, eng, a = _rt(clock=lambda: clock["t"], idle_timeout_s=120.0)
    rt.ensure_loaded()
    clock["t"] = 60.0
    assert rt.idle_reap() is False            # not idle long enough
    clock["t"] = 200.0
    assert rt.idle_reap() is True
    assert a.holder() is None and rt.state()["state"] == "cold"


def test_drain_timeout_hard_cuts_instead_of_hanging():
    # Codex review (018): a wedged in-flight request (the runtime lock held elsewhere) must not
    # let unload() block past the drain bound — hard-cut, llama `_unload_now` parity.
    import threading as _threading

    rt, eng, a = _rt()
    rt.ensure_loaded()
    holder_ready = _threading.Event()
    release = _threading.Event()

    def in_flight():                       # models a stuck request holding the runtime lock
        with rt.lock:
            holder_ready.set()
            release.wait(10)

    t = _threading.Thread(target=in_flight)
    t.start()
    holder_ready.wait(5)
    done = {}

    def unloader():
        done["res"] = rt.unload(drain_timeout_s=0.2)

    u = _threading.Thread(target=unloader)
    u.start()
    u.join(5)                              # must return within the bound — never hang
    assert not u.is_alive(), "unload() blocked past the drain bound"
    assert done["res"]["status"] == "unloaded" and done["res"]["drained"] is False
    assert a.holder() is None              # the hard cut freed the slot
    release.set()
    t.join(5)


def test_cpu_failed_load_is_cleaned_up_not_wedged():
    # Codex review (018): a CPU child that never becomes ready must be torn down like a GPU
    # one — otherwise it stays resident with last_used unset, invisible to the idle reaper.
    rt, eng, a = _rt(engine=FakeEngine(engine_id="embed", gpu=False), ready_wait_s=1.0)
    eng.ready_state = False
    try:
        rt.ensure_loaded()
    except lifecycle.EngineError:
        pass
    else:
        raise AssertionError("expected EngineError for a never-ready child")
    assert not eng.spawned[0].alive        # the failed child was actually torn down
    assert rt.state()["state"] == "cold"   # not stuck resident-but-unready


def test_cpu_engine_never_touches_admission():
    rt, eng, a = _rt(engine=FakeEngine(engine_id="embed", gpu=False))
    rt.ensure_loaded()
    assert a.holder() is None                 # off-lease by construction (Principle II exemption)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
