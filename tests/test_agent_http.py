"""018 US2 — the agent's HTTP routing (T352; Codex round 4, 018).

Offline, GPU-free: a real ThreadingHTTPServer on an ephemeral port over fake components. Pins
the round-4 routing fixes: `/jobs?kind=…` honors the contracted kind filter (the query string
used to defeat the exact match and return the unfiltered list), a stray `/jobsxyz` is a 404
(prefix match caught it), and `/jobs/{id}` detail + 404 still work.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent import lifecycle  # noqa: E402
from hostagent import main as agent_main  # noqa: E402
from hostagent.journal import Journal  # noqa: E402
from platformlib.contracts import AgentHealth, EngineState  # noqa: E402
from test_agent_lifecycle import FakeEngine  # noqa: E402 — the shared fake adapter


def _serve():
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(os.path.join(tempfile.mkdtemp(prefix="agent-http-"), "journal.jsonl"))
    journal.submit({"job_id": "t1", "kind": "train", "state": "queued", "submitted_at": 1.0})
    journal.submit({"job_id": "b1", "kind": "batch", "state": "queued", "submitted_at": 2.0})
    manager = lifecycle.EngineManager(admission, runtimes={})
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(
            admission, journal, manager, jobs_mod.JobManager(admission, journal)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_jobs_routing_query_filter_and_exact_match():
    server, base = _serve()
    try:
        code, body = _get(base, "/jobs")
        assert code == 200 and {j["job_id"] for j in body["jobs"]} == {"t1", "b1"}
        code, body = _get(base, "/jobs?kind=train")           # the contracted kind filter
        assert code == 200 and [j["job_id"] for j in body["jobs"]] == ["t1"]
        code, body = _get(base, "/jobs/t1")
        assert code == 200 and body["kind"] == "train"
        code, _ = _get(base, "/jobs/nope")
        assert code == 404
        code, _ = _get(base, "/jobsxyz")                      # prefix match used to swallow this
        assert code == 404
        code, _ = _get(base, "/health")
        assert code == 200
    finally:
        server.shutdown()


def _serve_with_engine():
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(os.path.join(tempfile.mkdtemp(prefix="agent-http-h"), "journal.jsonl"))
    rt = lifecycle.EngineRuntime(FakeEngine("fake"), admission, sleep=lambda s: None)
    manager = lifecycle.EngineManager(admission, runtimes={"fake": rt})
    rt.ensure_loaded()                                # holder=fake, engine ready
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(
            admission, journal, manager, jobs_mod.JobManager(admission, journal)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_health_conforms_to_agenthealth_contract():
    """019/US7 (FR-195): GET /health round-trips through AgentHealth with the GPU fields FLAT — a
    consumer must read the real holder/free-VRAM, not the defaults the prior nested `gpu` object was
    silently dropped to by the contract's unknown-field filter."""
    server, base = _serve_with_engine()
    try:
        code, body = _get(base, "/health")
        assert code == 200
        h = AgentHealth.from_json(body)
        assert h.holder == "fake"                     # flat field parsed (was None under the nesting)
        assert h.holder_kind == "serving"
        assert h.gpu_free_gb == 10.0
        assert h.wedged is False
        assert h.engines.get("fake") == "ready"
    finally:
        server.shutdown()


def test_engines_rows_conform_to_enginestate_contract():
    """019/US7 (FR-195): each GET /engines row carries engine_id so EngineState.from_json validates
    instead of raising ContractError on a bare {state: …} value."""
    server, base = _serve_with_engine()
    try:
        code, body = _get(base, "/engines")
        assert code == 200
        es = EngineState.from_json(body["engines"]["fake"])   # raised ContractError pre-fix
        assert es.engine_id == "fake" and es.state == "ready"
        assert es.gpu is True and es.optional is False
    finally:
        server.shutdown()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
