"""018 US2 — the durable job journal (T356/T357, FR-173, SC-109's offline half).

Offline, GPU-free. Pins: transitions fold to correct records across a simulated crash (a NEW
Journal over the same file — the dicts died with the "process"); non-terminal jobs are marked
`interrupted(reason)` at replay and counted (the restart alert); a torn tail line (crash
mid-write) doesn't poison the fold; terminal states survive verbatim.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent.journal import Journal  # noqa: E402


def _submit(j, job_id, kind="train", state="queued"):
    j.submit({"job_id": job_id, "kind": kind, "modality": "llm",
              "request": {"dataset_name": "d", "dataset_version": "v"},
              "state": state, "submitted_at": 1.0})


def test_records_survive_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "journal.jsonl")
        j1 = Journal(path)
        _submit(j1, "job-1")
        j1.transition("job-1", "running", started_at=2.0)
        j1.transition("job-1", "succeeded", ended_at=3.0, result={"version": "7"})
        _submit(j1, "job-2", kind="batch")
        j1.transition("job-2", "running", started_at=4.0)

        j2 = Journal(path)                        # the restart — in-memory state is gone
        done = j2.get("job-1")
        assert done["state"] == "succeeded" and done["result"] == {"version": "7"}
        assert j2.get("job-2")["state"] == "running"
        assert {r["job_id"] for r in j2.jobs()} == {"job-1", "job-2"}
        assert [r["job_id"] for r in j2.jobs(kind="batch")] == ["job-2"]


def test_interrupted_marking_counts_and_journals_the_alert():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "journal.jsonl")
        j1 = Journal(path)
        _submit(j1, "job-run")
        j1.transition("job-run", "running", started_at=2.0)
        _submit(j1, "job-queued")
        _submit(j1, "job-done")
        j1.transition("job-done", "succeeded", ended_at=3.0)

        j2 = Journal(path)
        n = j2.mark_interrupted("agent restart")
        assert n == 2                             # running + queued; succeeded untouched
        assert j2.get("job-run")["state"] == "interrupted"
        assert j2.get("job-run")["reason"] == "agent restart"
        assert j2.get("job-done")["state"] == "succeeded"
        assert j2.active_count() == 0

        j3 = Journal(path)                        # the interruption itself is durable
        assert j3.get("job-queued")["state"] == "interrupted"
        assert j3.mark_interrupted("agent restart") == 0   # idempotent across restarts


def test_torn_tail_line_does_not_poison_replay():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "journal.jsonl")
        j1 = Journal(path)
        _submit(j1, "job-1")
        j1.transition("job-1", "running", started_at=2.0)
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"ts": 5.0, "job_id": "job-1", "to": "succe')   # crash mid-write
        j2 = Journal(path)
        assert j2.get("job-1")["state"] == "running"       # the torn line is ignored, prefix holds


def test_unknown_fields_are_ignored_forward_compat():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "journal.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": 1.0, "job_id": "j", "to": "queued",
                                "record": {"job_id": "j", "kind": "train", "state": "queued",
                                           "submitted_at": 1.0},
                                "some_future_field": {"x": 1}}) + "\n")
        j = Journal(path)
        assert j.get("j")["state"] == "queued"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
