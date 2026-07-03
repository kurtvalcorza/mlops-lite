"""019 US4 (FR-192) — the agent's job launcher dedupes a re-issued retrain by its idempotency key.

Offline, GPU-free. The finding (T362 folded the trainer into `hostagent.jobs.JobManager`): a policy
retrain whose `/train` response was lost, or a park re-launched after a store blip, re-issues the SAME
request; without dedup the agent starts a SECOND job on the single GPU. With the key it returns the
existing job — exactly one job per breach. Exercises the pure decision (`_within_idempotency_window`)
and the `JobManager.submit` wiring.
"""
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent.journal import Journal  # noqa: E402

WINDOW = 900.0
FT_REQ = {"dataset_name": "d", "dataset_version": "1", "output_name": "o"}


# -- the pure decision --------------------------------------------------------------------------
def test_no_record_never_dedupes():
    assert jobs_mod._within_idempotency_window(None, 100.0, WINDOW) is False


def test_active_job_dedupes_regardless_of_window():
    assert jobs_mod._within_idempotency_window(
        {"state": "running", "submitted_at": 0.0}, 100000.0, WINDOW) is True   # lost response mid-run
    assert jobs_mod._within_idempotency_window(
        {"state": "queued", "submitted_at": 0.0}, 100000.0, WINDOW) is True


def test_recently_completed_dedupes_within_window():
    assert jobs_mod._within_idempotency_window(
        {"state": "succeeded", "submitted_at": 100.0}, 130.0, WINDOW) is True   # park retry, 30s later


def test_stale_completed_starts_fresh():
    assert jobs_mod._within_idempotency_window(
        {"state": "succeeded", "submitted_at": 100.0}, 100.0 + WINDOW + 1, WINDOW) is False


# -- the JobManager.submit wiring ---------------------------------------------------------------
def _jm(tmpdir, runner):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1e6, read_fn=lambda: 20.0), lease=None)
    journal = Journal(os.path.join(tmpdir, "journal.jsonl"))
    runners = {k: runner for k in jobs_mod.KINDS}
    return jobs_mod.JobManager(admission, journal, runners=runners)


def test_reissued_launch_with_same_key_dedupes_to_the_active_job(tmp_path):
    """A re-issued identical /train (same idempotency_key) while the first job still runs must return
    that job, not a 409 and not a second job on the single GPU."""
    release = threading.Event()

    def blocking_runner(jm, job_id, request):
        release.wait(3.0)                 # hold the job in `running` deterministically
        return {}

    jm = _jm(str(tmp_path), blocking_runner)
    try:
        code, body = jm.submit("finetune", {**FT_REQ, "idempotency_key": "k"})
        assert code == 202
        run_id = body["run_id"]
        # wait until it is actually running
        end = time.time() + 3
        while time.time() < end and (jm.get(run_id) or {}).get("state") != "running":
            time.sleep(0.02)

        code2, body2 = jm.submit("finetune", {**FT_REQ, "idempotency_key": "k"})
        assert code2 == 200 and body2.get("idempotent") is True and body2["run_id"] == run_id

        code3, _ = jm.submit("finetune", {**FT_REQ, "idempotency_key": "k2"})
        assert code3 == 409                # a DIFFERENT launch is still gated by the single job slot
    finally:
        release.set()


def test_reissued_launch_after_completion_dedupes_within_window(tmp_path):
    """Once the job completed, a park retry with the same key (within the window) returns the finished
    job rather than launching a duplicate; a different key starts fresh."""
    def instant_runner(jm, job_id, request):
        return {}

    jm = _jm(str(tmp_path), instant_runner)
    code, body = jm.submit("finetune", {**FT_REQ, "idempotency_key": "k"})
    assert code == 202
    run_id = body["run_id"]
    end = time.time() + 3
    while time.time() < end and (jm.get(run_id) or {}).get("state") not in ("succeeded", "failed"):
        time.sleep(0.02)

    code2, body2 = jm.submit("finetune", {**FT_REQ, "idempotency_key": "k"})
    assert code2 == 200 and body2["run_id"] == run_id and body2.get("idempotent") is True

    code3, body3 = jm.submit("finetune", {**FT_REQ, "idempotency_key": "k3"})
    assert code3 == 202 and body3["run_id"] != run_id   # a genuinely new retrain gets its own job


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
