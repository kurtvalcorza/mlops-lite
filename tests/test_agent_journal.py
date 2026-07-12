"""018 US2 → US4 T375-B — the durable job journal, now Postgres-backed (FR-173/FR-189, SC-109's
offline half).

Offline, GPU-free (a `FakeJobStore` stands in for the `jobs` table; it persists across Journal
instances, so a fresh `Journal(store=same_fake)` is a 'restart' that reconnects to the same rows).
Pins the semantics the cutover must preserve: records survive a restart via hydrate-from-table; the
terminal record (kind-specific fields + result) round-trips through the column split + the catch-all;
non-terminal jobs are marked `interrupted(reason)` at replay and counted (the restart alert) and that
is itself durable + idempotent; a durable-write failure never advances memory past the last committed
state (FR-189); a store blip self-heals on the next op.

The JSONL torn-tail / file-fold pins retired with the JSONL backend — Postgres owns durability now.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _agentstore import FakeJobStore  # noqa: E402

from hostagent.journal import Journal  # noqa: E402
from platformlib.store import StoreError  # noqa: E402


def _submit(j, job_id, kind="finetune", state="queued", submitted_at=1.0):
    j.submit({"job_id": job_id, "kind": kind, "modality": "llm",
              "request": {"dataset_name": "d", "dataset_version": "v"},
              "state": state, "submitted_at": submitted_at})


def test_records_survive_a_restart():
    store = FakeJobStore()
    j1 = Journal(store=store)
    _submit(j1, "job-1")
    j1.transition("job-1", "running", started_at=2.0)
    j1.transition("job-1", "succeeded", ended_at=3.0, result={"version": "7"},
                  fields={"mlflow_run_id": "abc", "model": "m@7"})
    _submit(j1, "job-2", kind="batch", submitted_at=2.0)
    j1.transition("job-2", "running", started_at=4.0)

    j2 = Journal(store=store)                 # the restart — in-memory state is gone, the rows aren't
    done = j2.get("job-1")
    assert done["state"] == "succeeded" and done["result"] == {"version": "7"}
    assert done["mlflow_run_id"] == "abc" and done["model"] == "m@7"   # catch-all round-trips
    assert done["started_at"] == 2.0 and done["ended_at"] == 3.0        # timestamptz float round-trip
    assert j2.get("job-2")["state"] == "running"
    assert "ended_at" not in j2.get("job-2")   # a running job has no ended_at column set (omitted)
    assert {r["job_id"] for r in j2.jobs()} == {"job-1", "job-2"}
    assert [r["job_id"] for r in j2.jobs(kind="batch")] == ["job-2"]


def test_jobs_listing_is_newest_first():
    store = FakeJobStore()
    j = Journal(store=store)
    _submit(j, "old", submitted_at=1.0)
    _submit(j, "new", submitted_at=9.0)
    assert [r["job_id"] for r in j.jobs()] == ["new", "old"]


def test_interrupted_marking_counts_and_is_durable_and_idempotent():
    store = FakeJobStore()
    j1 = Journal(store=store)
    _submit(j1, "job-run")
    j1.transition("job-run", "running", started_at=2.0)
    _submit(j1, "job-queued")
    _submit(j1, "job-done")
    j1.transition("job-done", "succeeded", ended_at=3.0)

    j2 = Journal(store=store)
    n = j2.mark_interrupted("agent restart")
    assert n == 2                              # running + queued; succeeded untouched
    assert j2.get("job-run")["state"] == "interrupted"
    assert j2.get("job-run")["reason"] == "agent restart"
    assert j2.get("job-done")["state"] == "succeeded"
    assert j2.active_count() == 0

    j3 = Journal(store=store)                  # the interruption itself is durable
    assert j3.get("job-queued")["state"] == "interrupted"
    assert j3.mark_interrupted("agent restart") == 0    # idempotent across restarts


def test_failed_store_write_does_not_advance_memory():
    """FR-189: a durable upsert that raises must leave in-memory state at the last committed
    transition — memory never leads the store."""
    store = FakeJobStore()
    j = Journal(store=store)
    _submit(j, "job-1")
    j.transition("job-1", "running", started_at=2.0)

    store.fail = True                          # the store goes down mid-op
    raised = False
    try:
        j.transition("job-1", "succeeded", ended_at=3.0)
    except StoreError:
        raised = True
    assert raised
    assert j.get("job-1")["state"] == "running"   # did not advance past the durable state
    assert j.active_count() == 1


def test_connection_self_heals_after_a_blip():
    store = FakeJobStore()
    j = Journal(store=store)
    _submit(j, "job-1")

    store.fail = True
    try:
        j.transition("job-1", "running", started_at=2.0)   # fails, drops the cached connection
    except StoreError:
        pass
    before = store.connects
    store.fail = False
    j.transition("job-1", "running", started_at=2.0)        # next op reconnects + succeeds
    assert store.connects == before + 1                     # a fresh connection was opened
    assert j.get("job-1")["state"] == "running"


def test_unreachable_store_at_startup_raises():
    store = FakeJobStore()
    store.fail = True
    raised = False
    try:
        Journal(store=store)                   # hydrate can't reach the store → fail loud
    except StoreError:
        raised = True
    assert raised


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
