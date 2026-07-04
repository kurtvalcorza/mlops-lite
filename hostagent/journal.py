"""Durable job journal (018 US2 → US4 T375-B, FR-173/FR-189) — restart-proof job records.

The trainer daemon kept `_runs/_studies/_batches/_shadows` in plain dicts: a restart lost every
record and the gateway 404'd on jobs that ran for many minutes (review §4.5). US2 made the agent
durable via a JSONL fold; **US4 (T375-B) moves that durability onto the resident Postgres `jobs`
table** (`platformlib.store`) — the last O(N)/file-bound piece of job state joins policies +
predictions in the relational store, and the JSONL path retires.

The design is unchanged in spirit, only the backend swaps:

  - The agent keeps its **authoritative in-memory job table** (`_jobs`), so the hot reads
    (`get`/`jobs`/`active_count`, polled by `/health` and the gateway) stay in-process — they never
    touch the store and so never fail (strictly better availability than the object scans US4 kills).
  - **Writes are durable-first (FR-189):** `submit`/`transition` upsert the FULL record to Postgres
    (autocommit — the row is committed) BEFORE advancing `_jobs`, so a failed write never leaves
    memory ahead of the store. A hard store failure RAISES (`StoreError`) — a job record is the job
    system's source of truth and must never be silently dropped (this is NOT the fail-open
    drop-counter path predictions/labels use).
  - **Restart recovery:** a fresh agent hydrates `_jobs` from `store.list_jobs` (the fold-from-disk,
    now a fold-from-table); `mark_interrupted` flips every still-queued/running job with ONE atomic
    `UPDATE … RETURNING` (FR-173) and updates the cache to match.

Connection: lazy + self-healing, mirroring gateway/app/policies.py — a store blip drops the
identity-scoped cached connection so the next op reconnects. The store is a hard dependency now
(Postgres is resident on the box); if it is unreachable at startup, hydration raises and the agent
crash-loops until the store is up (the supervisor restarts it), which is the correct fail-loud
posture for the job system's durable backend.
"""
import threading
import time


class Journal:
    def __init__(self, store=None, *, clock=time.time, dsn=None):
        from platformlib import store as _store_mod
        self._store = store if store is not None else _store_mod
        self.clock = clock
        self._dsn = dsn
        self._lock = threading.Lock()            # serializes writes + cache updates
        self._conn_lock = threading.Lock()       # guards the cached connection identity
        self._conn = None
        self._bootstrapped = False
        self._jobs = {}                          # job_id -> record dict (data-model.md §JobRecord)
        self._hydrate()

    # -- store connection (fail-loud, self-healing — mirrors gateway/app/policies.py) --------------
    def _connect(self):
        """The cached store connection, opened lazily + schema bootstrapped once. Raises the store's
        StoreError when the DB is unreachable (job ops must not silently no-op)."""
        with self._conn_lock:
            c = self._conn
            if c is not None and not getattr(c, "closed", False):
                return c
            c = self._store.connect(self._dsn)   # raises StoreError on failure
            if not self._bootstrapped:
                self._store.bootstrap(c)
                self._bootstrapped = True
            self._conn = c
            return c

    def _invalidate(self, bad_conn) -> None:
        """Drop+close the cached connection so the next op reconnects — identity-scoped so a stale
        invalidator can't close a healthy reconnection another thread established."""
        with self._conn_lock:
            c = self._conn
            if bad_conn is not None and c is not bad_conn:
                return
            self._conn = None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def _op(self, fn):
        """Run a store op on the cached connection; on any failure drop the connection (so a transient
        blip self-heals next call) and re-raise as the store's StoreError. Job writes are
        durable-first-or-raise — never the fail-open drop path."""
        conn = self._connect()
        try:
            return fn(conn)
        except self._store.StoreError:
            self._invalidate(conn)
            raise
        except Exception as e:  # noqa: BLE001 — normalize any driver/op error, then self-heal
            self._invalidate(conn)
            raise self._store.StoreError(f"job store operation failed: {e}") from e

    # -- rebuild from the store (the fold-from-disk, now a fold-from-table) ------------------------
    def _hydrate(self) -> None:
        with self._lock:
            rows = self._op(lambda c: self._store.list_jobs(c))
            self._jobs = {r["job_id"]: r for r in rows}

    def mark_interrupted(self, reason: str) -> int:
        """Startup pass (FR-173): every non-terminal job was killed with the previous agent. One
        atomic UPDATE flips them to `interrupted(reason)` and returns the count for the alert metric;
        the cache is updated to match. Idempotent across restarts (a second call flips nothing)."""
        ts = self.clock()
        with self._lock:
            flipped = self._op(lambda c: self._store.mark_jobs_interrupted(c, reason, ts))
            for job_id in flipped:
                rec = self._jobs.get(job_id)
                if rec is not None:
                    rec["state"] = "interrupted"
                    rec["reason"] = reason
                    rec["ended_at"] = ts
        return len(flipped)

    # -- writes (durable-first: upsert commits BEFORE the cache advances, FR-189) ------------------
    def submit(self, record: dict) -> None:
        """First sight of a job: durable-first upsert of the full record with its initial state."""
        with self._lock:
            rec = dict(record)
            self._op(lambda c: self._store.upsert_job(c, rec))
            self._jobs[record["job_id"]] = rec

    def transition(self, job_id: str, to: str, *, reason=None, started_at=None,
                   ended_at=None, result=None, fields=None) -> None:
        """Journal a state change. `fields` (018 T362) carries arbitrary kind-specific outputs
        (mlflow_run_id/model/metrics/summary/best/…) that must survive a restart alongside the state.
        The updated FULL record is upserted durable-first, so `_replay`'s successor (`list_jobs`)
        restores everything; the immutable columns (kind/modality/request/submitted_at) can't be
        shadowed by a caller field."""
        with self._lock:
            rec = dict(self._jobs.get(job_id) or {"job_id": job_id})
            rec["state"] = to
            extra = dict(fields or {})
            for k, v in (("reason", reason), ("started_at", started_at),
                         ("ended_at", ended_at), ("result", result)):
                if v is not None:
                    extra[k] = v
            for k, v in extra.items():
                # never let a caller's field shadow the identity/immutable columns
                if k in ("job_id", "kind", "modality", "request", "submitted_at", "state"):
                    continue
                rec[k] = v
            self._op(lambda c: self._store.upsert_job(c, rec))  # durable-first: raises before memory
            self._jobs[job_id] = rec

    # -- reads (served from the authoritative in-memory table — never touch the store) -------------
    def get(self, job_id: str):
        with self._lock:
            rec = self._jobs.get(job_id)
            return dict(rec) if rec else None

    def jobs(self, kind: str = None) -> list:
        with self._lock:
            rows = [dict(r) for r in self._jobs.values()
                    if kind is None or r.get("kind") == kind]
        rows.sort(key=lambda r: r.get("submitted_at") or 0, reverse=True)
        return rows

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._jobs.values() if r.get("state") in ("queued", "running"))
