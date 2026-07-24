"""Labels repository (024 US1, T566) — the write-once `labels` insert.

Lifted from `store.py` behind the `store` facade (call sites unchanged). Write-once (FR-185) is
enforced by the `labels` PRIMARY KEY, not an in-process lock: a duplicate insert raises `LabelExists`
(from a UniqueViolation). Label attach is operator-facing and fail-LOUD at the caller
(`QualityStoreError`→502) — unlike the fire-and-forget prediction/capture writes — so this primitive
PROPAGATES; it must never be made to swallow.
"""
from platformlib.storeimpl._base import LabelExists, _json


def attach_label(conn, prediction_id: str, label, submitted_at) -> None:
    """Insert a label, enforcing write-once via the PRIMARY KEY (FR-185). A duplicate raises
    LabelExists — the concurrent-safe replacement for the retired in-process `_label_write_lock`."""
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO labels (prediction_id, label, submitted_at) VALUES (%s, %s, %s)",
                (prediction_id, _json(label), submitted_at))
    except psycopg.errors.UniqueViolation as e:
        raise LabelExists(f"label already stored for {prediction_id}") from e
