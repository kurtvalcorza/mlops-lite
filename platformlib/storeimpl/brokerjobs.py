"""Broker jobs repository — the persisted FIFO lane (026 T647, T680, T685).

The jobs lane lives in Postgres, not in the host agent's memory, because FIFO that does not survive a
restart is not FIFO: a tenant whose job was second in line would silently lose its position on every
agent restart, and on a single-GPU broker restarts are ordinary events.

**Recovery is explicit, never a sweep** (T680). The pre-broker startup path
(`hostagent/journal.py`) atomically rewrites every `queued` and `running` job to `interrupted`, which
would empty the broker lane on every boot. Broker jobs are therefore recovered by their own rules:

  * `queued` jobs **stay queued, in their original `queue_pos` order** — they never occupied the GPU,
    so there is nothing to reconcile.
  * The single formerly-`running` job resolves to **`interrupted`** — never silently re-queued, since
    its sandbox is gone and re-running it is the tenant's decision, not the broker's.

`interrupted` is a distinct terminal state rather than a flavour of `failed` (T685). In a metered
multi-tenant broker that difference is the tenant's basis for disputing a charge: a broker-caused
restart and a tenant-code failure must not be indistinguishable after the fact.
"""
import uuid

from platformlib.storeimpl._base import StoreError, _epoch, _json

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")

_JOB_SELECT = ("SELECT id, tenant_id, kind, spec, state, queue_pos, sandbox, artifact_ref, "
               "model_version, gpu_seconds, created_at, started_at, ended_at FROM broker_jobs")


def _row(row) -> dict:
    (jid, tenant_id, kind, spec, state, queue_pos, sandbox, artifact_ref, model_version,
     gpu_seconds, created_at, started_at, ended_at) = row
    return {"id": jid, "tenant_id": tenant_id, "kind": kind, "spec": spec or {}, "state": state,
            "queue_pos": queue_pos, "sandbox": sandbox, "artifact_ref": artifact_ref,
            "model_version": model_version,
            "gpu_seconds": float(gpu_seconds) if gpu_seconds is not None else None,
            "created_at": _epoch(created_at), "started_at": _epoch(started_at),
            "ended_at": _epoch(ended_at)}


#: Transaction-scoped advisory lock guarding lane-position assignment. A row lock cannot serve here:
#: `SELECT max(queue_pos) … FOR UPDATE` is rejected outright (no row locks with aggregates), and
#: locking the current tail row does nothing when the lane is EMPTY — which is exactly when two
#: concurrent submissions would both compute position 1 and one would be rejected by the partial
#: unique index. A tenant losing a submission to a race is a worse failure than waiting a moment.
#: Transaction-scoped, so it is released on commit or rollback and a caller that dies cannot leak it.
_LANE_LOCK_KEY = 0x6272_6F6B_6572_6A62  # "brokerjb"


def enqueue_job(conn, tenant_id: str, kind: str, spec: dict, *, job_id: str = None,
                sandbox: str = "") -> dict:
    """Append a job to the tail of the lane."""
    job_id = job_id or str(uuid.uuid4())
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LANE_LOCK_KEY,))
            cur.execute("SELECT coalesce(max(queue_pos), 0) + 1 FROM broker_jobs "
                        "WHERE state = 'queued'")
            position = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO broker_jobs (id, tenant_id, kind, spec, state, queue_pos, sandbox) "
                "VALUES (%s, %s, %s, %s, 'queued', %s, %s)",
                (job_id, tenant_id, kind, _json(spec or {}), position, sandbox))
    return get_job(conn, job_id)


def get_job(conn, job_id: str):
    with conn.cursor() as cur:
        cur.execute(_JOB_SELECT + " WHERE id = %s", (job_id,))
        row = cur.fetchone()
    return _row(row) if row else None


def list_queued(conn) -> list:
    """The lane, in strict FIFO order."""
    with conn.cursor() as cur:
        cur.execute(_JOB_SELECT + " WHERE state = 'queued' ORDER BY queue_pos")
        return [_row(r) for r in cur.fetchall()]


def running_job(conn):
    with conn.cursor() as cur:
        cur.execute(_JOB_SELECT + " WHERE state = 'running'")
        row = cur.fetchone()
    return _row(row) if row else None


def list_jobs(conn, tenant_id: str = None, limit: int = 100) -> list:
    where, params = "", []
    if tenant_id:
        where, params = " WHERE tenant_id = %s", [tenant_id]
    with conn.cursor() as cur:
        cur.execute(_JOB_SELECT + where + " ORDER BY created_at DESC LIMIT %s",
                    (*params, int(limit)))
        return [_row(r) for r in cur.fetchall()]


def start_job(conn, job_id: str) -> dict:
    """`queued -> running`. Clears `queue_pos` (a running job holds no lane position) and is guarded
    on the current state, so a job cancelled between selection and start does not start anyway."""
    with conn.cursor() as cur:
        cur.execute("UPDATE broker_jobs SET state = 'running', queue_pos = NULL, "
                    "started_at = now() WHERE id = %s AND state = 'queued'", (job_id,))
        if cur.rowcount == 0:
            raise StoreError(f"job {job_id!r} is not queued")
    return get_job(conn, job_id)


def finish_job(conn, job_id: str, state: str, *, gpu_seconds: float = None,
               artifact_ref: str = None, model_version: str = None) -> dict:
    """Move a job to a terminal state. `running` is never forced back to `queued` — no preemption."""
    if state not in TERMINAL:
        raise StoreError(f"{state!r} is not a terminal job state")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE broker_jobs SET state = %s, queue_pos = NULL, ended_at = now(), "
            "gpu_seconds = coalesce(%s, gpu_seconds), "
            "artifact_ref = coalesce(%s, artifact_ref), "
            "model_version = coalesce(%s, model_version) "
            "WHERE id = %s AND state NOT IN ('succeeded','failed','cancelled','interrupted')",
            (state, gpu_seconds, artifact_ref, model_version, job_id))
    return get_job(conn, job_id)


def cancel_job(conn, job_id: str) -> dict:
    """Cancel a queued or running job, compacting the lane behind it.

    Idempotent: cancelling an already-terminal job returns it unchanged rather than raising, because
    a tenant retrying a cancel it is unsure landed must not be an error — and (T679) a cancel that
    settled its reservation each time would progressively exhaust the tenant's quota.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise StoreError(f"no such job {job_id!r}")
    if job["state"] in TERMINAL:
        return job
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE broker_jobs SET state = 'cancelled', queue_pos = NULL, "
                        "ended_at = now() WHERE id = %s", (job_id,))
        _compact(conn)
    return get_job(conn, job_id)


def _compact(conn) -> None:
    """Renumber the queued lane to 1..N, preserving order.

    Done in one statement against a window function so intermediate states never collide with the
    partial unique index — an update-in-a-loop would transiently assign a position another row still
    holds and be rejected.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE broker_jobs SET queue_pos = ranked.pos FROM ("
            "  SELECT id, row_number() OVER (ORDER BY queue_pos) + 1000000 AS pos "
            "  FROM broker_jobs WHERE state = 'queued') ranked "
            "WHERE broker_jobs.id = ranked.id")
        cur.execute(
            "UPDATE broker_jobs SET queue_pos = queue_pos - 1000000 WHERE state = 'queued'")


# -- owner override (T649) ------------------------------------------------------------------------

def reorder_job(conn, job_id: str, position: int) -> list:
    """Move a QUEUED job to a 1-based position. A running job is never touched.

    Raises `StoreError` for a running job rather than silently no-opping: an operator who typed the
    wrong id should be told, not left believing a reorder happened.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise StoreError(f"no such job {job_id!r}")
    if job["state"] != "queued":
        raise StoreError(f"job {job_id!r} is {job['state']}, not queued — a running job is never "
                         f"preempted or reordered")
    with conn.transaction():
        queued = [j["id"] for j in list_queued(conn)]
        queued.remove(job_id)
        index = max(0, min(len(queued), int(position) - 1))
        queued.insert(index, job_id)
        with conn.cursor() as cur:
            # Two passes through a disjoint high range, for the same collision reason as `_compact`.
            for offset, jid in enumerate(queued, start=1):
                cur.execute("UPDATE broker_jobs SET queue_pos = %s WHERE id = %s",
                            (offset + 1000000, jid))
            cur.execute("UPDATE broker_jobs SET queue_pos = queue_pos - 1000000 "
                        "WHERE state = 'queued'")
    return list_queued(conn)


def pin_job(conn, job_id: str) -> list:
    """Move a queued job to the head of the lane."""
    return reorder_job(conn, job_id, 1)


# -- restart recovery (T680) --------------------------------------------------------------------------

def recover_after_restart(conn) -> dict:
    """Resolve the lane after a host-agent restart.

    Returns `{"interrupted": [job_id], "queued": n}`. The caller settles each interrupted job's
    reservation to elapsed GPU-seconds and releases the remainder — left `reserved`, it would hold
    quota against the tenant forever.
    """
    interrupted = []
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id, started_at FROM broker_jobs WHERE state = 'running'")
            for job_id, started_at in cur.fetchall():
                interrupted.append({"id": job_id, "started_at": _epoch(started_at)})
            cur.execute("UPDATE broker_jobs SET state = 'interrupted', queue_pos = NULL, "
                        "ended_at = now() WHERE state = 'running'")
    # `queued` jobs are deliberately NOT touched: they keep their `queue_pos` order, which is the
    # whole reason the lane is persisted.
    return {"interrupted": interrupted, "queued": len(list_queued(conn))}
