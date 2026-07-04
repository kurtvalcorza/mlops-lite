"""018 US4 (T373) — the relational store client (contracts/store-schema.md).

Two layers:
  * **Offline (always run):** the DDL covers every contract table, `dsn()` resolution order, and the
    schema-version constant — pure assertions, no driver or DB needed (psycopg is imported lazily).
  * **Live (skip when the gateway DB is unreachable / psycopg absent):** `bootstrap()` is idempotent
    across two runs, a prediction⋈label round-trips through `window()`, a duplicate label raises
    `LabelExists` (the write-once PK, FR-185), and `capture_input` is idempotent. The live rows use a
    unique test-prefixed id and are DELETEd in a finally, so the drill leaves no residue.
"""
import os
import sys
import uuid

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from platformlib import store  # noqa: E402

# -- offline (no DB) -------------------------------------------------------------------------------

def test_ddl_covers_every_contract_table():
    for t in store.TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {t} " in store.DDL, t
    # the window ordering index + the jobs listing index must exist (SC-111 bounded scans)
    assert "ix_pred_window" in store.DDL and "ix_jobs_kind" in store.DDL
    assert store.SCHEMA_VERSION == 1


def test_dsn_prefers_full_url_then_builds_from_components(monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_URL", "postgresql://u:p@h:1/db")
    assert store.dsn() == "postgresql://u:p@h:1/db"
    monkeypatch.delenv("GATEWAY_DB_URL", raising=False)
    monkeypatch.setenv("POSTGRES_USER", "mlops")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("GATEWAY_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_DB_PORT", "55432")
    monkeypatch.setenv("GATEWAY_DB_NAME", "gateway")
    assert store.dsn() == "postgresql://mlops:secret@127.0.0.1:55432/gateway"


def test_connect_without_psycopg_or_db_raises_store_error(monkeypatch):
    # a bogus DSN → StoreError (never a raw psycopg exception the callers can't classify)
    monkeypatch.setenv("GATEWAY_DB_URL", "postgresql://nobody@127.0.0.1:1/nope")
    with pytest.raises(store.StoreError):
        store.connect()


# -- live (skip when the gateway DB is unreachable) ------------------------------------------------

def _live_dsn():
    """Build a host-reachable DSN from .env (Postgres is published on 127.0.0.1:55432)."""
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


def test_bootstrap_is_idempotent_across_two_runs(db):
    store.bootstrap(db)  # a second bootstrap must not error or duplicate the meta row
    with db.cursor() as cur:
        cur.execute("SELECT count(*), max(schema_version) FROM meta")
        n, ver = cur.fetchone()
    assert n == 1 and ver == store.SCHEMA_VERSION


def test_prediction_label_window_roundtrip_and_write_once(db):
    # Runs in the store's real autocommit mode (the connect() default): each write is its own
    # transaction, so a duplicate-label UniqueViolation aborts only that statement — no poisoning of
    # the next op (why the store needs no in-process _label_write_lock).
    from datetime import datetime, timezone
    pid = f"test-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    try:
        store.log_prediction(db, pid, "m", "v1", "llm", now, payload_ref="results/x")
        store.log_prediction(db, pid, "m", "v1", "llm", now)  # ON CONFLICT DO NOTHING → one row
        store.attach_label(db, pid, {"y": 1}, now)
        rows = store.window(db, "llm", "m", "v1", 50)
        mine = [r for r in rows if r["prediction_id"] == pid]
        assert len(mine) == 1 and mine[0]["label"] == {"y": 1}
        # write-once: a second label for the same prediction raises LabelExists (FR-185)
        with pytest.raises(store.LabelExists):
            store.attach_label(db, pid, {"y": 2}, now)
        # capture_index is idempotent
        store.capture_input(db, pid, "llm", "inputs/llm/x", now)
        store.capture_input(db, pid, "llm", "inputs/llm/x", now)
    finally:
        with db.cursor() as cur:  # leave no residue (FK order: children before the prediction)
            cur.execute("DELETE FROM labels WHERE prediction_id = %s", (pid,))
            cur.execute("DELETE FROM capture_index WHERE prediction_id = %s", (pid,))
            cur.execute("DELETE FROM predictions WHERE prediction_id = %s", (pid,))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
