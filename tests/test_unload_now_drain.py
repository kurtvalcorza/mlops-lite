"""017 US3 — supervisor `unload-now` drains in-flight work then releases (T338, FR-156, SC-103).

Offline + GPU-free: load `serving/llama/supervisor.py` (stdlib only; gpu_lease imports cleanly on Linux),
monkeypatch `_resident`/`_unload`, and drive `_unload_now` directly. The supervisor's GPU `_lock` is held
for the whole duration of an in-flight request, so a held lock models in-flight work: a clean drain
acquires it (`drained=True`); a request that outlasts the timeout forces a hard unload (`drained=False`).
"""
import importlib.util
import os
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_supervisor():
    path = os.path.join(REPO, "serving", "llama", "supervisor.py")
    spec = importlib.util.spec_from_file_location("llama_supervisor_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_resident(mod, state):
    mod._resident = lambda: state["resident"]


def _fake_unload(mod, state, calls):
    def _unload():
        calls.append("unload")
        state["resident"] = False  # the model proc is gone + lease released
    mod._unload = _unload


def test_idle_when_not_resident():
    mod = _load_supervisor()
    state, calls = {"resident": False}, []
    _fake_resident(mod, state)
    _fake_unload(mod, state, calls)
    assert mod._unload_now(10)["status"] == "idle"
    assert calls == []  # nothing to unload


def test_clean_drain_unloads_and_marks_drained():
    mod = _load_supervisor()
    state, calls = {"resident": True}, []
    _fake_resident(mod, state)
    _fake_unload(mod, state, calls)
    res = mod._unload_now(5)  # lock is free → immediate clean drain
    assert res == {"status": "unloaded", "drained": True}
    assert calls == ["unload"]


def test_inflight_that_finishes_within_timeout_drains():
    mod = _load_supervisor()
    state, calls = {"resident": True}, []
    _fake_resident(mod, state)
    _fake_unload(mod, state, calls)

    # Model an in-flight request: hold the GPU lock briefly, then release before the drain timeout.
    def inflight():
        with mod._lock:
            time.sleep(0.3)
    t = threading.Thread(target=inflight)
    t.start()
    time.sleep(0.05)  # ensure the in-flight thread grabs the lock first
    res = mod._unload_now(5)  # waits for the in-flight request, then unloads
    t.join()
    assert res["drained"] is True and calls == ["unload"]


def test_inflight_past_timeout_hard_unloads():
    mod = _load_supervisor()
    state, calls = {"resident": True}, []
    _fake_resident(mod, state)
    _fake_unload(mod, state, calls)

    stop = threading.Event()

    def stuck():
        with mod._lock:
            stop.wait(2.0)  # holds the lock longer than the drain timeout
    t = threading.Thread(target=stuck)
    t.start()
    time.sleep(0.05)
    res = mod._unload_now(0.2)  # can't acquire within 0.2s → hard unload
    assert res["status"] == "unloaded" and res["drained"] is False
    assert calls == ["unload"]  # still unloaded despite the stuck request (hard-cut)
    stop.set()
    t.join()


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
