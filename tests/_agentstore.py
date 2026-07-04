"""In-memory stand-in for `platformlib.store`'s JOB surface (the `jobs` table) — 018 US4 T375-B.

`hostagent.journal.Journal(store=FakeJobStore())` runs the whole durable-first / hydrate-on-start /
mark-interrupted path with no Postgres. The fake PERSISTS across Journal instances (a fresh
`Journal(store=same_fake)` is a 'restart' that reconnects to the same rows), and it goes through the
REAL `store._job_split` / `_job_row_to_record` (+ `_dt`/`_epoch`) so the column decomposition and the
epoch-float ↔ timestamptz round-trip are exercised for real — only the SQL/socket is faked. `fail`
flips it to a store outage (every op raises StoreError), so the fail-loud posture is testable.
"""
from platformlib import store as _store


class FakeJobStore:
    StoreError = _store.StoreError

    def __init__(self):
        self.rows = {}       # job_id -> the 9-tuple a jobs row holds (timestamps as datetime)
        self.fail = False    # flip on to simulate a store outage
        self.closed = False
        self.connects = 0    # how many times connect() ran (asserts the self-heal reconnected)

    # -- connection lifecycle -----------------------------------------------------------------------
    def connect(self, dsn=None, *, autocommit=True):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        self.connects += 1
        self.closed = False
        return self

    def close(self):
        self.closed = True

    def bootstrap(self, conn=None):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")

    # -- row (de)serialization via the real store helpers (faithful parity) -------------------------
    def _row(self, record):
        jid, kind, mod, req, state, sub, start, end, catch = _store._job_split(record)
        return (jid, kind, mod, req, state, _store._dt(sub), _store._dt(start), _store._dt(end),
                (catch or None))

    # -- writes -------------------------------------------------------------------------------------
    def upsert_job(self, conn, record):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        self.rows[record["job_id"]] = self._row(record)

    def import_job(self, conn, record):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        if record["job_id"] in self.rows:  # ON CONFLICT DO NOTHING
            return False
        self.rows[record["job_id"]] = self._row(record)
        return True

    def mark_jobs_interrupted(self, conn, reason, ended_at):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        flipped = []
        for jid, r in list(self.rows.items()):
            if r[4] in ("queued", "running"):
                catch = dict(r[8] or {})
                catch["reason"] = reason  # merged, not clobbering a partial result (|| in SQL)
                self.rows[jid] = (r[0], r[1], r[2], r[3], "interrupted", r[5], r[6],
                                  _store._dt(ended_at), catch)
                flipped.append(jid)
        return flipped

    # -- reads --------------------------------------------------------------------------------------
    def get_job(self, conn, job_id):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        r = self.rows.get(job_id)
        return _store._job_row_to_record(r) if r else None

    def list_jobs(self, conn, kind=None):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        recs = [_store._job_row_to_record(r) for r in self.rows.values()]
        if kind is not None:
            recs = [x for x in recs if x.get("kind") == kind]
        recs.sort(key=lambda x: x.get("submitted_at") or 0, reverse=True)
        return recs

    def count_active_jobs(self, conn):
        if self.fail:
            raise self.StoreError("gateway DB unreachable (fake)")
        return sum(1 for r in self.rows.values() if r[4] in ("queued", "running"))
