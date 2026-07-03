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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
