"""Predictions repository (024 US1, T565) — the `predictions` table + the predictions⋈labels window.

Lifted from `store.py` behind the `store` facade (call sites unchanged). The write is fail-OPEN at
the caller (the `quality` wrapper catches + drop-counts — the repo primitive itself PROPAGATES); the
window read is fail-LOUD at the caller. The `served_at DESC LIMIT n` join is one bounded index scan on
`ix_pred_window`, replacing the pre-US4 O(N) object listing (FR-186, SC-111).
"""


def log_prediction(conn, prediction_id: str, model_name: str, version: str, modality: str,
                   served_at, *, streamed: bool = False, payload_ref: str = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO predictions "
            "(prediction_id, model_name, version, modality, served_at, streamed, payload_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (prediction_id) DO NOTHING",
            (prediction_id, model_name, version, modality, served_at, streamed, payload_ref))


def window(conn, modality: str, model_name: str, version: str, n: int) -> list:
    """The `served_at DESC LIMIT n` predictions⋈labels join that replaces the O(N) object scan
    (FR-186, SC-111). Returns the most recent `n` LABELED predictions for the (modality, model,
    version) as dicts {prediction_id, served_at, payload_ref, label, submitted_at}. The composite
    index `ix_pred_window` serves the ordering, so this is a bounded index scan, not a listing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.prediction_id, p.served_at, p.payload_ref, l.label, l.submitted_at "
            "FROM predictions p JOIN labels l USING (prediction_id) "
            "WHERE p.modality = %s AND p.model_name = %s AND p.version = %s "
            "ORDER BY p.served_at DESC LIMIT %s",
            (modality, model_name, version, n))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def prediction_exists(conn, prediction_id: str) -> bool:
    """Whether a prediction row was logged (013 attach_label's 'unknown id' check — the write-once
    label FK also guards it, but this yields the explicit `unknown` status without an FK error)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM predictions WHERE prediction_id = %s", (prediction_id,))
        return cur.fetchone() is not None
