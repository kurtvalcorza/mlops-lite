"""T363 — gateway swap thinning: the AGENT-orchestrated preempt passthrough (offline, GPU-free).

017's gateway swap.py brokered preemption. T363 deletes it: the gateway just appends `?preempt=true`
and the AGENT orchestrates the evict→admit under its single admission lock — a `kind="job"` holder
refuses *structurally* (no network probe, so the gateway's fail-closed batch probe is gone). Drives
the agent's HTTP surface: the flag routes through `swap.preempt_for` → a resident holder is evicted
and the target loaded; refuse-if-held holds without the flag; a job holder is refused.
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from test_agent_lifecycle import FakeChild  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent import lifecycle  # noqa: E402
from hostagent import main as agent_main  # noqa: E402
from hostagent.journal import Journal  # noqa: E402
from _agentstore import FakeJobStore  # noqa: E402


class FakeGpuEngine:
    """A minimal GPU engine adapter (two of them share the single admission slot)."""
    def __init__(self, engine_id):
        self.engine_id = engine_id
        self.gpu = True
        self.optional = False
        self.verbs = ("go",)
        self.stream_verbs = ()
        self._children = []

    def available(self):
        return (True, None)

    def estimate_vram(self):
        return 1.0

    def spawn(self):
        c = FakeChild(pid=6000 + len(self._children))
        self._children.append(c)
        return c

    def ready(self):
        return True

    def forward(self, verb, body, load_ms):
        return {"engine": self.engine_id, "load_ms": load_ms}

    def health(self, resident):
        return {"ok": True, "resident": resident, "engine": self.engine_id}


def _serve():
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1e6, read_fn=lambda: 10.0))
    journal = Journal(store=FakeJobStore())
    # idle_timeout inf + no reaper thread started (make_handler doesn't) → engines stay resident.
    rts = {eid: lifecycle.EngineRuntime(FakeGpuEngine(eid), admission, idle_timeout_s=float("inf"))
           for eid in ("llm", "vision")}
    manager = lifecycle.EngineManager(admission, runtimes=rts)
    jobs = jobs_mod.JobManager(admission, journal)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(admission, journal, manager, jobs))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", admission, jobs


def _post(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    r = urllib.request.Request(base + path, data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_preempt_evicts_resident_serving_holder_and_loads_target():
    server, base, admission, _ = _serve()
    try:
        assert _post(base, "/engines/llm/go")[0] == 200            # llm becomes the resident holder
        assert admission.holder()["tenant"] == "llm"
        code, body = _post(base, "/engines/vision/go?preempt=true")  # agent orchestrates the swap
        assert code == 200 and body["engine"] == "vision"
        assert admission.holder()["tenant"] == "vision"            # llm evicted, vision resident
    finally:
        server.shutdown()


def test_refuse_if_held_without_preempt():
    server, base, admission, _ = _serve()
    try:
        _post(base, "/engines/llm/go")
        code, body = _post(base, "/engines/vision/go")             # no flag → 008 refuse-if-held
        assert code == 409 and "Principle II" in body["error"]
        assert admission.holder()["tenant"] == "llm"               # holder unchanged
    finally:
        server.shutdown()


def test_preempt_refuses_job_holder_structurally():
    server, base, admission, _ = _serve()
    try:
        admission.acquire("training", "job", 1.0)          # a training/HPO/batch job holds the slot
        code, body = _post(base, "/engines/llm/go?preempt=true")   # a job is never preempted
        assert code == 409 and "job" in body["error"].lower()
        assert admission.holder()["tenant"] == "training"          # job untouched
    finally:
        server.shutdown()


def test_preempt_refuses_batch_driven_serving_holder():
    # @claude PR#37 (FR-155): a GPU batch drives a serving engine WITHOUT a kind="job" slot — the
    # engine holds admission as kind="serving", so the job-holder check misses it. The agent threads
    # JobManager._gpu_batch_active into preempt_for, which refuses evicting a batch-driven holder.
    server, base, admission, jobs = _serve()
    try:
        jobs._gpu_batch_active = True                      # a GPU batch is driving a serving engine
        _post(base, "/engines/llm/go")                     # llm holds admission as kind="serving"
        assert admission.holder()["kind"] == "serving"
        code, body = _post(base, "/engines/vision/go?preempt=true")
        assert code == 409 and "batch" in body["error"].lower()
        assert admission.holder()["tenant"] == "llm"       # batch-driven holder untouched
    finally:
        server.shutdown()


def test_preempt_flag_parses_only_truthy_values():
    server, base, admission, _ = _serve()
    try:
        _post(base, "/engines/llm/go")
        # `preempt=0` is falsey → still refuse-if-held (not a swap)
        code, _ = _post(base, "/engines/vision/go?preempt=0")
        assert code == 409 and admission.holder()["tenant"] == "llm"
    finally:
        server.shutdown()


# 019/US3 note: the retired gateway swap's `_default_target_probe` HTTP serve-ready check (FR-191 —
# don't evict a working holder for a target that would 503 anyway) is subsumed by the agent's
# `preempt_for`, which loads the target AFTER evicting and, on a load failure, RETARGETS the swap
# reservation back to the holder (rollback) — a stronger guarantee than a pre-probe, so that gateway
# test retired with `gateway/app/swap.py` at T363.


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
