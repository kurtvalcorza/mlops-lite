"""Capture-index repository (024 US1, T567) — the `capture_index` rows + the shadow-replay resolution.

Lifted from `store.py` behind the `store` facade (call sites unchanged). The capture write is fail-OPEN
at the caller (the `quality` wrapper catches + drop-counts — the repo primitive PROPAGATES); the reads
are fail-LOUD at the caller. `replay_window` is the shadow-replay (016) resolution join
(capture∩predictions∩labels), bounded by the `capture_index` PK + the predictions window index.
"""


def capture_input(conn, prediction_id: str, modality: str, input_ref: str, captured_at) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capture_index (prediction_id, modality, input_ref, captured_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (prediction_id) DO NOTHING",
            (prediction_id, modality, input_ref, captured_at))


def capture_exists(conn, prediction_id: str) -> bool:
    """Whether a capture-index row exists for this prediction (symmetric with `prediction_exists`) —
    the backfill's idempotency precheck so a re-run counts an already-migrated input as skipped."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM capture_index WHERE prediction_id = %s", (prediction_id,))
        return cur.fetchone() is not None


def replay_window(conn, modality: str, model_name: str, version: str, n: int,
                  *, ttl_cutoff=None) -> list:
    """The shadow-replay (016) resolution path: the most recent `n` captured∩labeled predictions for
    (modality, model, version), newest first, as dicts {prediction_id, input_ref, payload_ref, label,
    captured_at}. `input_ref` locates the recoverable served input (for re-running the challenger);
    `payload_ref` the champion's logged output. `ttl_cutoff` (a timestamptz) drops captures older than
    the retention window IN THE QUERY (FR-186) — no per-object key parse. Indexed by `capture_index`
    PK + the predictions window index; bounded gets follow on only these `n` rows."""
    sql = ("SELECT c.prediction_id, c.input_ref, p.payload_ref, l.label, c.captured_at "
           "FROM capture_index c "
           "JOIN predictions p USING (prediction_id) "
           "JOIN labels l USING (prediction_id) "
           "WHERE c.modality = %s AND p.model_name = %s AND p.version = %s")
    params = [modality, model_name, version]
    if ttl_cutoff is not None:
        sql += " AND c.captured_at >= %s"
        params.append(ttl_cutoff)
    sql += " ORDER BY c.captured_at DESC LIMIT %s"
    params.append(n)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        cols = [col.name for col in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def capture_rows(conn, modality: str) -> list:
    """All `(prediction_id, input_ref, captured_at)` capture-index rows for a modality — the input to
    the cap/TTL prune (the successor to listing `inputs/<modality>/` objects)."""
    with conn.cursor() as cur:
        cur.execute("SELECT prediction_id, input_ref, captured_at FROM capture_index "
                    "WHERE modality = %s", (modality,))
        return [{"prediction_id": r[0], "input_ref": r[1], "captured_at": r[2]}
                for r in cur.fetchall()]


def delete_capture(conn, prediction_id: str) -> None:
    """Drop a capture-index row (the object body is deleted separately) — used by the prune."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_index WHERE prediction_id = %s", (prediction_id,))


def has_captures(conn, modality: str) -> bool:
    """Whether ANY recoverable input is captured for `modality` (shadow-replay corpus presence)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM capture_index WHERE modality = %s LIMIT 1", (modality,))
        return cur.fetchone() is not None
