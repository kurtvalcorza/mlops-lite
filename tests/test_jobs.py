"""018 T362 — the training daemon folded into the host agent's jobs surface (offline, GPU-free).

Drives `hostagent.jobs.JobManager` against a real journal + a real in-process `Admission` (fake
GpuReader, no lease), with injected runners/`spawn` so no torch/optuna/subprocess is needed. Pins:
per-kind validation, the single job slot (409 busy), GPU admission as `kind="job"` (held→409,
VRAM→507), batch off-lease + `gpu_batch_active`, the subprocess runner plumbing (set_child + result
mapping), the durable journal lifecycle, `legacy_view` byte-compat, `health_fields`, and cancel.
"""
import json
import os
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _agentstore import FakeJobStore  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent.journal import Journal  # noqa: E402

FT_REQ = {"dataset_name": "d", "dataset_version": "1", "output_name": "o"}


def _admission(free_gb=20.0):
    return adm.Admission(vram_budget_gb=12.0,
                         gpu=adm.GpuReader(ttl_s=1e6, read_fn=lambda: free_gb))


def _journal(tmp_path=None):
    # US4 T375-B: the journal is Postgres-backed now — a fresh in-memory FakeJobStore per journal
    # (tmp_path is vestigial; kept so the many call sites don't churn).
    return Journal(store=FakeJobStore())


def _const_runners(update):
    """A runner set that completes immediately with `update` — no heavy flow imports."""
    def run(jm, job_id, request):
        return dict(update)
    return {k: run for k in jobs_mod.KINDS}


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    return pred()


def _wait_state(jm, job_id, state, timeout=3.0):
    return _wait(lambda: (jm.get(job_id) or {}).get("state") == state and jm.get(job_id), timeout)


class FakeProc:
    """Stand-in subprocess: writes the result file the runner reads (argv[3]), then 'exits' 0."""
    def __init__(self, cmd, *, result, **kw):
        self.pid = 4242
        self.returncode = 0
        if result is not None:
            with open(cmd[3], "w", encoding="utf-8") as f:
                json.dump(result, f)

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


# ---- validation (400s, order: fields before modality) -----------------------------------------
def test_unknown_kind_400(tmp_path):
    code, p = jobs_mod.JobManager(_admission(), _journal(tmp_path)).submit("nope", {})
    assert code == 400 and "unknown job kind" in p["error"]


def test_missing_field_400(tmp_path):
    code, p = jobs_mod.JobManager(_admission(), _journal(tmp_path)).submit(
        "finetune", {"dataset_name": "d"})
    assert code == 400 and "missing field" in p["error"]


def test_unknown_modality_400(tmp_path):
    code, p = jobs_mod.JobManager(_admission(), _journal(tmp_path)).submit(
        "finetune", {**FT_REQ, "modality": "xyz"})
    assert code == 400 and "unknown modality" in p["error"]


# ---- the single job slot (one job at a time) --------------------------------------------------
def test_single_job_slot_then_free(tmp_path):
    started, release = threading.Event(), threading.Event()

    def run(jm, job_id, request):
        started.set()
        release.wait(3.0)
        return {"status": "completed"}

    jm = jobs_mod.JobManager(_admission(), _journal(tmp_path),
                             runners={k: run for k in jobs_mod.KINDS})
    code1, p1 = jm.submit("finetune", dict(FT_REQ))
    assert code1 == 202 and "run_id" in p1
    assert started.wait(2.0)
    code2, p2 = jm.submit("finetune", dict(FT_REQ))          # busy while the first runs
    assert code2 == 409 and "busy" in p2["error"]
    release.set()
    assert _wait_state(jm, p1["run_id"], "completed")
    code3, _ = jm.submit("finetune", dict(FT_REQ))            # slot freed → accepted
    assert code3 == 202


# ---- GPU admission as kind="job" --------------------------------------------------------------
def test_gpu_job_acquires_admission_kind_job(tmp_path):
    started, release = threading.Event(), threading.Event()

    def run(jm, job_id, request):
        started.set()
        release.wait(3.0)
        return {"status": "completed"}

    a = _admission()
    jm = jobs_mod.JobManager(a, _journal(tmp_path), runners={k: run for k in jobs_mod.KINDS})
    jm.submit("finetune", dict(FT_REQ))
    assert started.wait(2.0)
    holder = a.holder()
    assert holder["tenant"] == "training" and holder["kind"] == "job"
    release.set()


def test_admission_held_returns_409(tmp_path):
    a = _admission()
    a.acquire("vision", "serving", 1.0)                       # a serving tenant holds the slot
    code, p = jobs_mod.JobManager(a, _journal(tmp_path),
                                  runners=_const_runners({"status": "completed"})).submit(
        "finetune", dict(FT_REQ))
    assert code == 409 and p["holder"] == "vision"


def test_vram_exceeded_returns_507(tmp_path):
    a = _admission(free_gb=0.5)                               # only 0.5 GB free
    code, p = jobs_mod.JobManager(a, _journal(tmp_path), est_gb=4.0,
                                  runners=_const_runners({"status": "completed"})).submit(
        "finetune", dict(FT_REQ))
    assert code == 507 and "VRAM" in p["error"]


# ---- batch: off-lease + gpu_batch_active flag (FR-155) ----------------------------------------
def _batch_flag_after_submit(tmp_path, modality):
    started, release = threading.Event(), threading.Event()

    def run(jm, job_id, request):
        started.set()
        release.wait(3.0)
        return {"status": "succeeded", "result": {}}

    a = _admission()
    jm = jobs_mod.JobManager(a, _journal(tmp_path), runners={k: run for k in jobs_mod.KINDS})
    jm.submit("batch", {"dataset_name": "d", "dataset_version": "1", "model": "m",
                        "modality": modality})
    started.wait(2.0)
    holder, flag = a.holder(), jm.health_fields()["gpu_batch_active"]
    release.set()
    return holder, flag


def test_batch_gpu_modality_flags_active_and_takes_no_lease(tmp_path):
    holder, flag = _batch_flag_after_submit(tmp_path, "llm")
    assert holder is None and flag is True                    # off-lease, marked non-preemptable


def test_batch_tabular_is_off_gpu_no_flag(tmp_path):
    holder, flag = _batch_flag_after_submit(tmp_path, "tabular")
    assert holder is None and flag is False


def test_terminal_journal_write_precedes_slot_release(tmp_path):
    """@claude PR#33: the durable terminal transition must land BEFORE the GPU slot/_active free —
    else a crash in the window loses a completed job (FR-173) and /health reads busy=false while
    jobs_active still counts it. A spy journal snapshots the slot state at the terminal write."""
    a = _admission()
    seen = {}

    class SpyJournal(Journal):
        def transition(self, job_id, to, **kw):
            if to in ("completed", "succeeded", "failed"):   # snapshot at the TERMINAL write
                seen["active"] = jm._active
                seen["holder"] = a.holder()
            return super().transition(job_id, to, **kw)

    jm = jobs_mod.JobManager(a, SpyJournal(store=FakeJobStore()),
                             runners=_const_runners({"status": "completed"}))
    _, p = jm.submit("finetune", dict(FT_REQ))
    assert _wait_state(jm, p["run_id"], "completed")
    # at the terminal journal write the job slot was STILL held — release runs strictly after
    assert seen["active"] == p["run_id"]
    assert seen["holder"] is not None and seen["holder"]["kind"] == "job"
    # and afterwards the slot is freed (no strand)
    assert a.holder() is None and jm._active is None


# ---- store blips must never strand the slot/lease (@claude PR#46, US4 T375-B) -----------------
def test_submit_store_failure_releases_gpu_lease(tmp_path):
    """The journal write is a networked Postgres upsert now — if it fails AFTER the GPU lease is
    taken, `submit` must release the lease before propagating, else the next submit is wrongly 409'd
    by a lease no live job holds."""
    a = _admission()
    store = FakeJobStore()
    jm = jobs_mod.JobManager(a, Journal(store=store),         # hydrated while the store is up
                             runners=_const_runners({"status": "completed"}))
    store.fail = True                                         # the store goes down mid-submit
    with pytest.raises(store.StoreError):
        jm.submit("finetune", dict(FT_REQ))
    assert a.holder() is None and jm._active is None          # lease + slot freed, not stranded


def test_worker_running_transition_failure_frees_slot(tmp_path):
    """The worker's first `transition(running)` is the same networked write. A blip there fires
    BEFORE the terminal try/finally, so without the guard the daemon thread would die holding the
    slot + GPU lease → every later submit 409s forever. The guard frees them; the durable record
    stays `queued` for the next restart's `mark_interrupted` to reconcile."""
    class FailRunningStore(FakeJobStore):
        def upsert_job(self, conn, record):
            if record.get("state") == "running":             # queued-write ok, running-write blips
                raise self.StoreError("blip on running-transition (fake)")
            return super().upsert_job(conn, record)

    a = _admission()
    jm = jobs_mod.JobManager(a, Journal(store=FailRunningStore()),
                             runners=_const_runners({"status": "completed"}))
    code, p = jm.submit("finetune", dict(FT_REQ))
    assert code == 202                                        # the queued-write landed
    assert _wait(lambda: a.holder() is None and jm._active is None, 3.0)   # slot/lease freed
    assert (jm.get(p["run_id"]) or {})["state"] == "queued"   # record never advanced past queued


# ---- subprocess runner plumbing (real _run_finetune, fake Popen) ------------------------------
def test_finetune_subprocess_success_maps_fields_and_sets_child(tmp_path):
    seen = {}

    def spawn(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(cmd, result={"ok": True, "result": {
            "run_id": "mlf1", "model": {"name": "m", "version": "7", "source": "s3://x"},
            "metrics": {"acc": 0.9}, "params": {"lr": 1e-4}}})

    a = _admission()
    jm = jobs_mod.JobManager(a, _journal(tmp_path), spawn=spawn)   # REAL runners
    code, p = jm.submit("finetune", dict(FT_REQ))
    assert code == 202
    rec = _wait_state(jm, p["run_id"], "completed")
    assert rec["mlflow_run_id"] == "mlf1" and rec["model"]["version"] == "7"
    assert rec["metrics"] == {"acc": 0.9}
    assert seen["cmd"][1].endswith("run_flow.py")             # ran the CUDA-isolation subprocess
    assert a.holder() is None                                 # released on completion


def test_finetune_subprocess_crash_no_result_is_failed(tmp_path):
    def spawn(cmd, **kw):
        return FakeProc(cmd, result=None)                     # child died without writing a result

    jm = jobs_mod.JobManager(_admission(), _journal(tmp_path), spawn=spawn)
    code, p = jm.submit("finetune", dict(FT_REQ))
    rec = _wait_state(jm, p["run_id"], "failed")
    assert rec["state"] == "failed" and "without a valid result" in rec["error"]


# ---- shadow: client-supplied id is the job id -------------------------------------------------
def test_shadow_uses_client_shadow_id(tmp_path):
    jm = jobs_mod.JobManager(
        _admission(), _journal(tmp_path),
        runners=_const_runners({"status": "succeeded", "result": {"winner": "x"}}))
    code, p = jm.submit("shadow", {"name": "n", "challenger": "2", "champion_version": "1",
                                   "modality": "vision", "shadow_id": "sid-123"})
    assert code == 202 and p["shadow_id"] == "sid-123"
    assert _wait_state(jm, "sid-123", "succeeded")


# ---- durable journal lifecycle + restart replay -----------------------------------------------
def test_journal_records_and_replays():
    store = FakeJobStore()
    jm = jobs_mod.JobManager(_admission(), Journal(store=store),
                             runners=_const_runners({"status": "completed", "mlflow_run_id": "r9",
                                                     "model": {"version": "4"}}))
    code, p = jm.submit("finetune", dict(FT_REQ))
    _wait_state(jm, p["run_id"], "completed")
    # A fresh Journal hydrating the same table rebuilds the terminal record incl. the result fields.
    replayed = Journal(store=store).get(p["run_id"])
    assert replayed["state"] == "completed" and replayed["mlflow_run_id"] == "r9"
    assert replayed["model"]["version"] == "4"


# ---- legacy_view byte-compat ------------------------------------------------------------------
def test_legacy_view_per_kind_ids_and_status():
    fv = jobs_mod.legacy_view({"job_id": "a", "kind": "finetune", "state": "completed",
                               "model": {"version": "5"}, "metrics": {"x": 1}})
    assert fv["run_id"] == "a" and fv["status"] == "completed" and fv["model"]["version"] == "5"
    bv = jobs_mod.legacy_view({"job_id": "b", "kind": "batch", "state": "succeeded",
                               "result": "s3://out"})
    assert bv["batch_id"] == "b" and bv["result"] == "s3://out"
    sv = jobs_mod.legacy_view({"job_id": "c", "kind": "shadow", "state": "succeeded"})
    assert sv["shadow_id"] == "c"


def test_legacy_view_interrupted_maps_to_failed():
    v = jobs_mod.legacy_view({"job_id": "a", "kind": "hpo", "state": "interrupted"})
    assert v["study_id"] == "a" and v["status"] == "failed"


# ---- health fields (trainer-compat superset) --------------------------------------------------
def test_health_fields_idle(tmp_path):
    h = jobs_mod.JobManager(_admission(free_gb=10.0), _journal(tmp_path)).health_fields()
    assert h["busy"] is False and h["active"] is None and h["gpu_batch_active"] is False
    assert h["gpu_free_mib"] == int(10.0 * 1024) and h["vram_budget_gb"] == 12.0


# ---- cancel -----------------------------------------------------------------------------------
def test_cancel_unknown_job(tmp_path):
    r = jobs_mod.JobManager(_admission(), _journal(tmp_path)).cancel("nope")
    assert r["status"] == "unknown"


def test_cancel_in_process_job_not_cancellable(tmp_path):
    started, release = threading.Event(), threading.Event()

    def run(jm, job_id, request):
        started.set()
        release.wait(3.0)
        return {"status": "completed"}

    jm = jobs_mod.JobManager(_admission(), _journal(tmp_path),
                             runners={k: run for k in jobs_mod.KINDS})
    _, p = jm.submit("hpo", dict(FT_REQ))            # HPO runs in-process — no killable child
    started.wait(2.0)
    assert jm.cancel(p["study_id"])["status"] == "not-cancellable"
    release.set()


def test_cancel_subprocess_terminates_child(tmp_path):
    class BlockingProc:
        def __init__(self, cmd, **kw):
            self.pid = 555
            self.returncode = 0
            self._ev = threading.Event()

        def wait(self, timeout=None):
            self._ev.wait(3.0)
            return 0

        def poll(self):
            return 0 if self._ev.is_set() else None

        def terminate(self):
            self._ev.set()

        def kill(self):
            self._ev.set()

    procs = []

    def spawn(cmd, **kw):
        pr = BlockingProc(cmd)
        procs.append(pr)
        return pr

    jm = jobs_mod.JobManager(_admission(), _journal(tmp_path), spawn=spawn)
    _, p = jm.submit("finetune", dict(FT_REQ))
    assert _wait(lambda: p["run_id"] in jm._children, 2.0)    # child registered
    assert jm.cancel(p["run_id"])["status"] == "cancelling"
    # terminate() fired → wait() returns → no result file → failed run, slot released
    assert _wait_state(jm, p["run_id"], "failed")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
