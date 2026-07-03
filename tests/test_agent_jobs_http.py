"""018 T362 — the agent's jobs HTTP surface (offline, GPU-free).

A real ThreadingHTTPServer over a JobManager with an injected instant runner drives the routing the
jobs fold-in added: the new `POST /jobs` (+ `GET /jobs?kind`), the legacy trainer aliases
(`POST/GET /train|/study|/batch|/shadow-replay`) rendered byte-compatibly, the wrong-kind 404, the
`/health` superset the trainer's consumers read, and cancel.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
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


def _instant_runners(update):
    def run(jm, job_id, request):
        return dict(update)
    return {k: run for k in jobs_mod.KINDS}


def _serve(update):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1e6, read_fn=lambda: 8.0), lease=None)
    journal = Journal(os.path.join(tempfile.mkdtemp(prefix="agent-jobs-"), "journal.jsonl"))
    jobs = jobs_mod.JobManager(admission, journal, runners=_instant_runners(update))
    manager = lifecycle.EngineManager(admission, runtimes={})
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(admission, journal, manager, jobs))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", jobs


def _req(url, *, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _wait_state(jobs, job_id, state, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        rec = jobs.get(job_id)
        if rec and rec.get("state") == state:
            return rec
        time.sleep(0.02)
    return jobs.get(job_id)


def test_post_jobs_nested_body_submits():
    server, base, jobs = _serve({"status": "completed", "model": {"version": "3"}})
    try:
        code, p = _req(base + "/jobs", method="POST", body={
            "kind": "finetune", "modality": "llm",
            "request": {"dataset_name": "d", "dataset_version": "1", "output_name": "o"}})
        assert code == 202 and "run_id" in p
        rec = _wait_state(jobs, p["run_id"], "completed")
        assert rec["kind"] == "finetune" and rec["modality"] == "llm"
    finally:
        server.shutdown()


def test_legacy_train_alias_roundtrip_byte_compat():
    server, base, jobs = _serve({"status": "completed", "mlflow_run_id": "r1",
                                 "model": {"name": "m", "version": "5", "source": "s"},
                                 "metrics": {"acc": 1.0}})
    try:
        code, p = _req(base + "/train", method="POST", body={
            "dataset_name": "d", "dataset_version": "1", "output_name": "o", "modality": "llm"})
        assert code == 202 and "run_id" in p and p["status"] == "queued"
        rid = p["run_id"]
        _wait_state(jobs, rid, "completed")
        code, rec = _req(base + f"/train/{rid}")
        assert code == 200
        # trainer byte-compat: run_id key, `status` (not `state`), top-level model/metrics/mlflow id
        assert rec["run_id"] == rid and rec["status"] == "completed"
        assert rec["model"]["version"] == "5" and rec["mlflow_run_id"] == "r1"
        assert rec["metrics"] == {"acc": 1.0} and "state" not in rec
    finally:
        server.shutdown()


def test_legacy_get_unknown_and_wrong_kind_404():
    server, base, jobs = _serve({"status": "completed"})
    try:
        assert _req(base + "/train/nope")[0] == 404
        # a finetune id fetched via the /study alias must 404 (kind mismatch), as the trainer did
        _, p = _req(base + "/train", method="POST", body={
            "dataset_name": "d", "dataset_version": "1", "output_name": "o"})
        assert _req(base + f"/study/{p['run_id']}")[0] == 404
    finally:
        server.shutdown()


def test_get_jobs_listing_filtered_by_kind():
    server, base, jobs = _serve({"status": "succeeded", "result": {}})
    try:
        _req(base + "/batch", method="POST", body={
            "dataset_name": "d", "dataset_version": "1", "model": "m", "modality": "tabular"})
        code, body = _req(base + "/jobs?kind=batch")
        assert code == 200 and body["jobs"] and body["jobs"][0]["kind"] == "batch"
        assert _req(base + "/jobs?kind=finetune")[1]["jobs"] == []
    finally:
        server.shutdown()


def test_health_superset_carries_trainer_fields():
    server, base, jobs = _serve({"status": "completed"})
    try:
        code, h = _req(base + "/health")
        assert code == 200
        for k in ("busy", "active", "gpu_batch_active", "gpu_free_mib", "vram_budget_gb"):
            assert k in h, f"missing trainer-compat health field {k!r}"
        assert h["gpu_free_mib"] == int(8.0 * 1024) and h["busy"] is False
    finally:
        server.shutdown()


def test_cancel_route_unknown_is_404():
    server, base, jobs = _serve({"status": "completed"})
    try:
        code, r = _req(base + "/jobs/nope/cancel", method="POST", body={})
        assert code == 404 and r["status"] == "unknown"
    finally:
        server.shutdown()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
