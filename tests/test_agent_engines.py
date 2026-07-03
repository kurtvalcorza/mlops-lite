"""018 T358 — the agent's generic /engines forward surface (offline, GPU-free).

Pins `hostagent.main.forward_engine`: resolve engine → admit (cold-load) under the runtime lock →
forward → stamp last_used, with the preserved 008–017 error vocabulary (404/409/507/503/400/502).
Adding an engine touches no routing code (SC-114) — the fake engine drives the same path.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from test_agent_lifecycle import FakeChild  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle, main  # noqa: E402


class FwdEngine:
    """A fake serving adapter with the forward surface the agent routes to."""

    engine_id = "llm"
    gpu = True
    optional = False
    verbs = ("infer",)
    stream_verbs = ("infer",)

    def __init__(self):
        self.forwarded = []

    def available(self):
        return (True, None)

    def estimate_vram(self):
        return 1.0

    def spawn(self):
        return FakeChild()

    def ready(self):
        return True

    def forward(self, verb, body, load_ms):
        self.forwarded.append((verb, body, load_ms))
        return {"text": "ok", "load_ms": load_ms, "model": "m"}


def _mgr(*, free_gb=10.0, engine=None):
    a = adm.Admission(vram_budget_gb=12.0,
                      gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: free_gb))
    rt = lifecycle.EngineRuntime(engine or FwdEngine(), a, sleep=lambda s: None)
    return lifecycle.EngineManager(a, {"llm": rt}), a


def test_forward_engine_happy_path_admits_forwards_and_touches():
    mgr, a = _mgr()
    code, payload = main.forward_engine(mgr, "llm", "infer", {"prompt": "x"})
    assert code == 200 and payload["text"] == "ok" and payload["load_ms"] >= 0.0
    assert a.holder()["tenant"] == "llm"                 # admitted the slot
    assert mgr.runtimes["llm"].last_used is not None     # last_used stamped
    assert mgr.runtimes["llm"].adapter.forwarded[0][0] == "infer"


def test_forward_engine_unknown_engine_is_404():
    mgr, _ = _mgr()
    code, payload = main.forward_engine(mgr, "nope", "infer", {})
    assert code == 404 and "nope" in payload["error"]


def test_forward_engine_unknown_verb_is_404():
    mgr, _ = _mgr()
    code, payload = main.forward_engine(mgr, "llm", "classify", {})
    assert code == 404 and "classify" in payload["error"]


def test_forward_engine_busy_holder_is_409_with_holder():
    mgr, a = _mgr()
    a.acquire("vision", "serving", est_gb=2.0)  # another tenant holds the single slot
    code, payload = main.forward_engine(mgr, "llm", "infer", {"prompt": "x"})
    assert code == 409 and payload["holder"] == "vision" and payload["kind"] == "serving"


def test_forward_engine_vram_exceeded_is_507():
    mgr, _ = _mgr(free_gb=0.1)  # estimate 1.0 > 0.1 free
    code, payload = main.forward_engine(mgr, "llm", "infer", {"prompt": "x"})
    assert code == 507 and "VRAM" in payload["error"]


def test_forward_engine_job_holder_is_never_evicted_here_409():
    # a training job holds the slot — a plain forward 409s (preempt is gateway-fronted)
    mgr, a = _mgr()
    a.acquire("training", "job", est_gb=6.0)
    code, payload = main.forward_engine(mgr, "llm", "infer", {"prompt": "x"})
    assert code == 409 and payload["holder"] == "training" and payload["kind"] == "job"


def test_error_status_mapping():
    assert main._engine_error_status(adm.VramExceeded("x")) == 507
    assert main._engine_error_status(lifecycle.EngineError("x")) == 503
    assert main._engine_error_status(ValueError("x")) == 400
    assert main._engine_error_status(RuntimeError("x")) == 502


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
