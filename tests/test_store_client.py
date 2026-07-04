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


# -- US4 T375: policies + pending + status + suggestions -------------------------------------------

def test_ddl_folds_pending_and_status_onto_policies():
    assert "ADD COLUMN IF NOT EXISTS pending" in store.DDL
    assert "ADD COLUMN IF NOT EXISTS status" in store.DDL


def test_policy_pending_status_roundtrip(db):
    from datetime import datetime, timezone
    m = f"test-policy-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).timestamp()
    try:
        store.put_policy(db, m, {"model_name": m, "modality": "vision", "updated_at": now},
                         now, "operator")
        assert store.get_policy(db, m)["modality"] == "vision"
        assert m in [p["model_name"] for p in store.list_policies(db)]
        # pending + status ride the SAME row; a policy re-`put` must NOT wipe them (ON CONFLICT keeps them)
        store.set_pending(db, m, {"attempts": 1})
        store.set_status(db, m, {"last_check_at": now})
        store.put_policy(db, m, {"model_name": m, "modality": "vision", "updated_at": now}, now, "op2")
        assert store.get_pending(db, m) == {"attempts": 1}
        assert store.get_status(db, m)["last_check_at"] == now
        store.clear_pending(db, m)
        assert store.get_pending(db, m) is None
        store.delete_policy(db, m)  # pending + status go with the row
        assert store.get_policy(db, m) is None
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM policies WHERE model_name = %s", (m,))


def test_suggestion_lifecycle_and_atomic_resolve(db):
    from datetime import datetime, timezone
    sid = f"test-sug-{uuid.uuid4().hex[:8]}"
    m = f"test-model-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).timestamp()
    rec = {"id": sid, "model_name": m, "candidate_version": "7", "gate_verdict": {"verdict": "pass"},
           "shadow_verdict": None, "state": "open", "created_at": now, "resolved_at": None,
           "actor": None}
    try:
        store.create_suggestion(db, rec)
        got = store.get_suggestion(db, sid)
        assert got["candidate_version"] == "7" and got["gate_verdict"] == {"verdict": "pass"}
        assert abs(got["created_at"] - now) < 1e-3  # epoch float round-trips through timestamptz
        # idempotency lookup is per (model, candidate, state)
        assert store.find_suggestion(db, m, "7", "open")["id"] == sid
        assert store.find_suggestion(db, m, "7", "accepted") is None
        assert [s["id"] for s in store.list_suggestions(db, state="open", model_name=m)] == [sid]
        # atomic resolve: the `WHERE state='open'` guard admits exactly one — the second sees 0 rows
        upd = store.resolve_suggestion(db, sid, "accepted", "operator", now)
        assert upd["state"] == "accepted" and upd["actor"] == "operator"
        assert store.resolve_suggestion(db, sid, "dismissed", "op2", now) is None
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM suggestions WHERE id = %s", (sid,))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
