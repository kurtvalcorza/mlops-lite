"""018 T376 — scripts/backfill_store.py (pre-US4 Garage objects → the gateway Postgres store).

Two layers, mirroring test_store_client.py:
  * **Offline (always run):** the JSONL journal fold (`_fold_journal`) rebuilds the final record per
    job from the transition log, and drops a crash-torn tail line.
  * **Live (skip when the gateway DB is unreachable):** a full seeded object set (FakeS3) migrates
    into every table, spot-checked through the store's own readers, and a SECOND run inserts nothing
    (idempotent — the migration's core guarantee). Every row uses a unique test id and is DELETEd in
    a finally, so the drill leaves no residue.
"""
import json
import os
import sys
import time
import uuid

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "scripts"))

import backfill_store as bf  # noqa: E402
from _quality import FakeS3  # noqa: E402
from platformlib import store  # noqa: E402


# -- offline: the journal fold ---------------------------------------------------------------------

def test_fold_journal_rebuilds_terminal_record(tmp_path):
    path = tmp_path / "journal.jsonl"
    lines = [
        {"ts": 1.0, "job_id": "j1", "to": "queued",
         "record": {"job_id": "j1", "kind": "finetune", "modality": "llm",
                    "request": {"dataset_name": "d"}, "state": "queued", "submitted_at": 1.0,
                    "run_id": "j1"}},
        {"ts": 2.0, "job_id": "j1", "from": "queued", "to": "running", "started_at": 2.0},
        {"ts": 3.0, "job_id": "j1", "from": "running", "to": "completed", "ended_at": 3.0,
         "mlflow_run_id": "r1", "model": {"version": "5"}, "metrics": {"acc": 1.0}},
        {"ts": 1.5, "job_id": "j2", "to": "queued",       # a second job that never left queued
         "record": {"job_id": "j2", "kind": "batch", "modality": "tabular", "state": "queued",
                    "submitted_at": 1.5}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
        f.write("{ torn tail line\n")               # crash mid-write — dropped whole

    folded = {r["job_id"]: r for r in bf._fold_journal(str(path))}
    assert set(folded) == {"j1", "j2"}
    j1 = folded["j1"]
    assert j1["state"] == "completed" and j1["started_at"] == 2.0 and j1["ended_at"] == 3.0
    assert j1["mlflow_run_id"] == "r1" and j1["model"]["version"] == "5"
    assert j1["kind"] == "finetune" and j1["submitted_at"] == 1.0   # from the submit's record
    assert folded["j2"]["state"] == "queued"


def test_fold_journal_missing_file_is_empty():
    assert bf._fold_journal(None) == []
    assert bf._fold_journal("/no/such/journal.jsonl") == []


# -- live: full object set migrates + idempotent re-run --------------------------------------------

def _live_dsn():
    pw = ""
    try:
        for line in open(os.path.join(REPO, ".env"), encoding="utf-8"):
            if line.startswith("POSTGRES_PASSWORD="):
                pw = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    user = os.getenv("POSTGRES_USER", "mlops")
    port = os.getenv("GATEWAY_DB_PORT", "55432")
    return f"postgresql://{user}:{pw}@127.0.0.1:{port}/gateway"


@pytest.fixture
def db():
    try:
        conn = store.connect(_live_dsn())
    except store.StoreError as e:
        pytest.skip(f"gateway DB not reachable — bring the stack up (up_all): {e}")
    store.bootstrap(conn)
    yield conn
    conn.close()


def _seed(s3, sfx, now):
    """Seed one of each object kind under a unique suffix; return the ids for asserts + cleanup."""
    pid1, pid2 = f"bf-{sfx}-a", f"bf-{sfx}-b"          # pid1 labeled+captured, pid2 streamed
    model = f"bf-model-{sfx}"
    sid = f"bf-sug-{sfx}"
    job_id = f"bf-job-{sfx}"

    def put(key, obj):
        s3.put_object(Bucket=bf.RESULTS_BUCKET, Key=key, Body=json.dumps(obj).encode())

    put(f"predictions/{pid1}.json", {"prediction_id": pid1, "model_name": model,
        "model_version": 1, "modality": "llm", "ts": now, "input_ref": None,
        "prediction": {"text": "hi"}})
    put(f"predictions/{pid2}.json", {"prediction_id": pid2, "model_name": model,
        "model_version": 1, "modality": "llm", "ts": now, "input_ref": None, "prediction": None})
    put(f"labels/{pid1}.json", {"prediction_id": pid1, "label": {"y": 1}, "ts": now})
    ikey = f"inputs/llm/{int(now * 1000):015d}-{pid1}.json"
    put(ikey, {"prediction_id": pid1, "modality": "llm", "input": {"prompt": "hi"}, "ts": now})
    put(f"policies/{model}.json", {"model_name": model, "modality": "vision",
        "updated_at": now, "updated_by": "operator"})
    put(f"policies/_pending/{model}.json", {"model_name": model, "dataset_version": "3"})
    put(f"policies/_status/{model}.json", {"last_check": now, "verdict": "ok"})  # body has no name
    put(f"suggestions/{sid}.json", {"id": sid, "model_name": model, "candidate_version": 7,
        "gate_verdict": {"passed": True}, "shadow_verdict": None, "state": "open",
        "created_at": now, "resolved_at": None, "actor": None})
    return {"pid1": pid1, "pid2": pid2, "model": model, "sid": sid, "job_id": job_id}


def _write_journal(tmp_path, job_id, now):
    path = tmp_path / "journal.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "job_id": job_id, "to": "queued", "record": {
            "job_id": job_id, "kind": "finetune", "modality": "llm",
            "request": {"dataset_name": "d"}, "state": "queued", "submitted_at": now,
            "run_id": job_id}}) + "\n")
        f.write(json.dumps({"ts": now + 1, "job_id": job_id, "from": "running", "to": "completed",
                            "ended_at": now + 1, "mlflow_run_id": "r1",
                            "model": {"version": "5"}}) + "\n")
    return str(path)


def test_backfill_migrates_every_table_and_is_idempotent(db, tmp_path):
    sfx = uuid.uuid4().hex[:10]
    now = time.time()
    s3 = FakeS3()
    ids = _seed(s3, sfx, now)
    journal = _write_journal(tmp_path, ids["job_id"], now)
    try:
        r1 = bf.backfill_all(s3, db, journal_path=journal)
        # first pass: every table inserts exactly its seeded rows, no errors
        assert [r1[t]["errors"] for t in r1] == [0] * len(r1), r1
        assert r1["predictions"]["inserted"] == 2 and r1["predictions"]["scanned"] == 2
        assert r1["labels"]["inserted"] == 1
        assert r1["capture"]["inserted"] == 1
        assert r1["policies"]["inserted"] == 1 and r1["policies"]["scanned"] == 1   # sub-prefixes skipped
        assert r1["pending"]["inserted"] == 1
        assert r1["status"]["inserted"] == 1
        assert r1["suggestions"]["inserted"] == 1
        assert r1["jobs"]["inserted"] == 1

        # spot-check the rows through the store's own readers
        win = [x for x in store.window(db, "llm", ids["model"], "1", 50)
               if x["prediction_id"] == ids["pid1"]]
        assert len(win) == 1 and win[0]["label"] == {"y": 1}
        assert store.capture_exists(db, ids["pid1"]) is True
        assert store.get_policy(db, ids["model"])["modality"] == "vision"
        assert store.get_pending(db, ids["model"]) is not None
        assert store.get_status(db, ids["model"]) is not None
        sug = store.get_suggestion(db, ids["sid"])
        assert sug is not None and sug["candidate_version"] == "7"   # coerced to text
        job = store.get_job(db, ids["job_id"])
        assert job["state"] == "completed" and job["mlflow_run_id"] == "r1"

        # second pass: nothing new — every row already migrated (idempotent, no clobber)
        r2 = bf.backfill_all(s3, db, journal_path=journal)
        for t, c in r2.items():
            assert c["inserted"] == 0 and c["errors"] == 0, (t, c)
            assert c["skipped"] == c["scanned"], (t, c)
    finally:
        with db.cursor() as cur:                          # FK order: children before predictions
            cur.execute("DELETE FROM labels WHERE prediction_id = ANY(%s)",
                        ([ids["pid1"], ids["pid2"]],))
            cur.execute("DELETE FROM capture_index WHERE prediction_id = ANY(%s)",
                        ([ids["pid1"], ids["pid2"]],))
            cur.execute("DELETE FROM predictions WHERE prediction_id = ANY(%s)",
                        ([ids["pid1"], ids["pid2"]],))
            cur.execute("DELETE FROM suggestions WHERE id = %s", (ids["sid"],))
            cur.execute("DELETE FROM policies WHERE model_name = %s", (ids["model"],))
            cur.execute("DELETE FROM jobs WHERE job_id = %s", (ids["job_id"],))


def test_backfill_counts_errors_on_orphan_object(db):
    """A malformed/orphan object increments `errors` (and drives main()'s non-zero exit) without
    aborting the run: an orphan label — no matching prediction row — hits the FK and is counted, not
    raised. Nothing is inserted, so there's no residue to clean."""
    sfx = uuid.uuid4().hex[:10]
    pid = f"bf-orphan-{sfx}"
    s3 = FakeS3()
    s3.put_object(Bucket=bf.RESULTS_BUCKET, Key=f"labels/{pid}.json",
                  Body=json.dumps({"prediction_id": pid, "label": {"y": 1},
                                   "ts": time.time()}).encode())
    report = bf.backfill_all(s3, db, journal_path=None)
    assert report["labels"] == {"scanned": 1, "inserted": 0, "skipped": 0, "errors": 1}
    assert any(c["errors"] for c in report.values())        # ⇒ main() would exit non-zero


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
