"""Policy repository (024 US1, T569) — the `policies` rows + queue-of-one pending + check status.

Lifted from `store.py` behind the `store` facade (call sites unchanged, US3/FR-179). The pre-US4
policy state rode Garage objects (policies/*.json + _pending/ + _status/); here those become indexed
table reads/updates. Timestamps stay epoch FLOATS in the record (the UI + pre-US4 shape); the
timestamptz columns exist for ordering; `_dt` converts at the seam.
"""
from platformlib.storeimpl._base import _dt, _json

# -- policies (FR-179) -----------------------------------------------------------------------------

def put_policy(conn, model_name: str, document: dict, updated_at, updated_by: str) -> None:
    """Upsert the validated ModelPolicy `document`. ON CONFLICT touches only document/updated_at/
    updated_by — a re-declaration must NOT wipe the row's pending/status (they outlive a re-`put`,
    matching the pre-US4 separate-object behavior)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO policies (model_name, document, updated_at, updated_by) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (model_name) DO UPDATE SET "
            "document = EXCLUDED.document, updated_at = EXCLUDED.updated_at, "
            "updated_by = EXCLUDED.updated_by",
            (model_name, _json(document), _dt(updated_at), updated_by))


def get_policy(conn, model_name: str):
    """The stored ModelPolicy document (epoch-float timestamps intact), or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT document FROM policies WHERE model_name = %s", (model_name,))
        row = cur.fetchone()
        return row[0] if row else None


def list_policies(conn) -> list:
    """Every policy document, name-ordered — one indexed scan of the `policies` table (no O(N) object
    listing, no `_pending`/`_status` sub-prefix filtering)."""
    with conn.cursor() as cur:
        cur.execute("SELECT document FROM policies ORDER BY model_name")
        return [r[0] for r in cur.fetchall()]


def delete_policy(conn, model_name: str) -> None:
    """Drop the policy row — its pending + status columns go with it (no separate deletes)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM policies WHERE model_name = %s", (model_name,))


# -- queue-of-one pending retrain + per-model check status (columns on the policy row) --------------

def set_pending(conn, model_name: str, pending: dict) -> None:
    """Park the queue-of-one retrain on the policy row. UPDATE-only: a pending for a policy that no
    longer exists is meaningless (the pre-US4 orphan `_pending` object can't happen)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE policies SET pending = %s WHERE model_name = %s",
                    (_json(pending), model_name))


def get_pending(conn, model_name: str):
    with conn.cursor() as cur:
        cur.execute("SELECT pending FROM policies WHERE model_name = %s", (model_name,))
        row = cur.fetchone()
        return row[0] if row else None  # None both when no policy and when nothing is parked


def clear_pending(conn, model_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE policies SET pending = NULL WHERE model_name = %s", (model_name,))


def set_status(conn, model_name: str, status: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE policies SET status = %s WHERE model_name = %s",
                    (_json(status), model_name))


def get_status(conn, model_name: str):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM policies WHERE model_name = %s", (model_name,))
        row = cur.fetchone()
        return row[0] if row else None
