"""Jobs repository (024 US1, T568) — the durable `jobs` table behind the host-agent journal (US4 T375-B).

Lifted from `store.py` behind the `store` facade (call sites unchanged). The `jobs` table IS the
durable log: the agent writes durable-first (an upsert per transition) BEFORE advancing its in-memory
table, hydrates from `list_jobs` on start, and flips crash-orphaned jobs with one atomic
`mark_jobs_interrupted`. The flat JobRecord bag maps to typed columns + a `result` jsonb CATCH-ALL
carrying every OTHER key (kind-specific outputs, the failure `reason`, the id_key alias) so a record
round-trips losslessly. Record timestamps are epoch floats; the columns are timestamptz; `_dt`/`_epoch`
convert at the seam.
"""
from platformlib.storeimpl._base import _dt, _epoch, _json

_JOB_RESERVED = ("job_id", "kind", "modality", "request", "state",
                 "submitted_at", "started_at", "ended_at")

_JOB_SELECT = ("SELECT job_id, kind, modality, request, state, submitted_at, started_at, "
               "ended_at, result FROM jobs")


def _job_split(record: dict):
    """A flat JobRecord bag -> the column tuple. Every key that is NOT a typed column or `request`
    lands in the `result` catch-all, so kind-specific fields + `reason` + the id_key alias all
    survive the round-trip (the DB has no column for them individually)."""
    catch = {k: v for k, v in record.items() if k not in _JOB_RESERVED}
    return (record["job_id"], record.get("kind") or "", record.get("modality") or "",
            record.get("request") or {}, record.get("state") or "queued",
            record.get("submitted_at"), record.get("started_at"), record.get("ended_at"), catch)


def _job_row_to_record(row) -> dict:
    """A jobs row -> the flat JobRecord dict. started_at/ended_at are set only when present (the
    pre-US4 record omits them until a transition sets them) and the `result` catch-all is spread back
    to the top level — reproducing the journalled record shape exactly (byte-compat, FR-177)."""
    (job_id, kind, modality, request, state, submitted_at, started_at, ended_at, result) = row
    rec = {"job_id": job_id, "kind": kind, "modality": modality, "request": request or {},
           "state": state, "submitted_at": _epoch(submitted_at)}
    if started_at is not None:
        rec["started_at"] = _epoch(started_at)
    if ended_at is not None:
        rec["ended_at"] = _epoch(ended_at)
    if result:
        rec.update(result)  # catch-all: reason + kind-specific outputs + the id_key alias
    return rec


def _job_insert(conn, record: dict, on_conflict: str) -> int:
    jid, kind, modality, request, state, sub, start, end, catch = _job_split(record)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (job_id, kind, modality, request, state, submitted_at, "
            "started_at, ended_at, result) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            + on_conflict,
            (jid, kind, modality, _json(request), state, _dt(sub), _dt(start), _dt(end),
             _json(catch) if catch else None))
        return cur.rowcount


def upsert_job(conn, record: dict) -> None:
    """Durable-first write of the FULL record (INSERT ... ON CONFLICT DO UPDATE). The agent calls this
    BEFORE advancing its in-memory table, so a failed write never leaves memory ahead of the store —
    the JSONL fsync-before-memory guarantee (FR-189), now a committed row."""
    _job_insert(conn, record,
                "ON CONFLICT (job_id) DO UPDATE SET kind = EXCLUDED.kind, "
                "modality = EXCLUDED.modality, request = EXCLUDED.request, state = EXCLUDED.state, "
                "submitted_at = EXCLUDED.submitted_at, started_at = EXCLUDED.started_at, "
                "ended_at = EXCLUDED.ended_at, result = EXCLUDED.result")


def import_job(conn, record: dict) -> bool:
    """Backfill insert (T376): the same split, but ON CONFLICT DO NOTHING (an already-migrated job is
    left untouched). Returns True iff a row was inserted (for the migration's counts)."""
    return _job_insert(conn, record, "ON CONFLICT (job_id) DO NOTHING") > 0


def get_job(conn, job_id: str):
    with conn.cursor() as cur:
        cur.execute(_JOB_SELECT + " WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        return _job_row_to_record(row) if row else None


def list_jobs(conn, kind: str = None) -> list:
    """Every job record, newest-first (optionally one kind) — the `ix_jobs_kind` index serves the
    (kind, submitted_at DESC) ordering. Hydrates the agent's in-memory table on start."""
    sql = _JOB_SELECT
    params = ()
    if kind is not None:
        sql += " WHERE kind = %s"
        params = (kind,)
    sql += " ORDER BY submitted_at DESC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_job_row_to_record(r) for r in cur.fetchall()]


def count_active_jobs(conn) -> int:
    """Jobs still queued or running — the `hostagent_jobs_active` gauge + the /health field."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')")
        return cur.fetchone()[0]


def mark_jobs_interrupted(conn, reason: str, ended_at) -> list:
    """Flip every still-queued/running job to `interrupted` in ONE atomic UPDATE at agent startup
    (FR-173): a crash killed them with the previous agent. Returns the flipped job_ids (for the alert
    count + the in-memory table update). `reason` folds into the `result` catch-all (merged, not
    clobbering any partial result); `ended_at` sets the column."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET state = 'interrupted', ended_at = %s, "
            # cast the bound param to jsonb: _json binds as `json`, and `jsonb || json` has no operator
            "result = coalesce(result, '{}'::jsonb) || %s::jsonb "
            "WHERE state IN ('queued', 'running') RETURNING job_id",
            (_dt(ended_at), _json({"reason": reason})))
        return [r[0] for r in cur.fetchall()]
