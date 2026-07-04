"""018 US4 (T374) — write-once labels under concurrency (FR-185, quickstart §US4).

The in-process `_label_write_lock` is gone; write-once is now the `labels` PRIMARY KEY. This pins the
property that MATTERS under that change: when many duplicate label submissions for the same prediction
race, EXACTLY ONE is stored and the rest are cleanly reported "duplicate" — never two labels, never a
crash, never a silent overwrite of served history.

Two layers:
  * **Offline (always run):** N threads hammer `quality.attach_label` for one prediction through the
    FakeStore (whose write-once check is lock-guarded like the real PK) — a 100-trial style race.
  * **Live (skip when the gateway DB is unreachable):** the same race against the REAL Postgres PK,
    each thread on its own connection, so the constraint itself is exercised. Rows are DELETEd after.
"""
import os
import sys
import threading
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _quality import install_fakes, load_quality  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- offline: the FakeStore PK, under real threads -------------------------------------------------

def _race_attach(q, pid, n):
    """Fire `n` threads each attaching a distinct label to `pid`; return the list of status strings."""
    results = []
    lock = threading.Lock()
    start = threading.Barrier(n)

    def worker(i):
        start.wait()  # release all threads together to maximize the overlap on the write
        status = q.attach_label(pid, {"y": i})["status"]
        with lock:
            results.append(status)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_duplicate_labels_store_exactly_one():
    q = load_quality()
    for trial in range(100):  # 100-trial style: the race must resolve the same way every time
        install_fakes(q)  # fresh FakeStore each trial
        pid = f"p{trial}"
        q._store.log_prediction(q._conn(), pid, None, "1", "vision",
                                datetime.now(timezone.utc), payload_ref=f"predictions/{pid}.json")
        results = _race_attach(q, pid, n=8)
        assert results.count("attached") == 1, results          # exactly one winner
        assert results.count("duplicate") == 7                   # the rest cleanly rejected
        assert pid in q._store.labels                            # and a label really landed
        assert "unknown" not in results                          # the prediction was known throughout


# -- live: the real Postgres PRIMARY KEY, under real threads ---------------------------------------

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


def test_live_label_pk_stores_exactly_one_under_concurrency():
    from platformlib import store
    try:
        conn = store.connect(_live_dsn())
    except store.StoreError as e:
        pytest.skip(f"gateway DB not reachable — bring the stack up (up_all): {e}")
    store.bootstrap(conn)
    pid = f"test-writeonce-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    outcomes, lock, start = [], threading.Lock(), threading.Barrier(8)

    def worker(i):
        c = store.connect(_live_dsn())  # each thread its own connection (autocommit)
        try:
            start.wait()
            store.attach_label(c, pid, {"y": i}, now)
            outcome = "attached"
        except store.LabelExists:
            outcome = "duplicate"
        finally:
            c.close()
        with lock:
            outcomes.append(outcome)

    try:
        store.log_prediction(conn, pid, "m", "1", "vision", now, payload_ref="predictions/x")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("attached") == 1, outcomes   # the PK admitted exactly one writer
        assert outcomes.count("duplicate") == 7
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM labels WHERE prediction_id = %s", (pid,))
            assert cur.fetchone()[0] == 1                   # one row in the table, no residue race
    finally:
        with conn.cursor() as cur:  # FK order: label child before the prediction
            cur.execute("DELETE FROM labels WHERE prediction_id = %s", (pid,))
            cur.execute("DELETE FROM predictions WHERE prediction_id = %s", (pid,))
        conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
