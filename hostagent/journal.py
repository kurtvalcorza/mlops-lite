"""Durable job journal (018 US2, FR-173, research R9) — restart-proof job records.

The trainer daemon kept `_runs/_studies/_batches/_shadows` in plain dicts: a restart lost every
record and the gateway 404'd on jobs that ran for many minutes (review §4.5). The agent appends
one JSONL line per state transition to `STATE_DIR/journal.jsonl` and folds the file left at
startup to rebuild the table. Any job found `queued`/`running` at replay was interrupted by the
crash/restart: it is marked `interrupted(reason)` with a NEW journalled transition and counted —
surfaced as an alert metric + health field, never silent (clarify Q2). Writes move to the
relational store at US4 (T375); the JSONL path retires with it.

Journal line: {"ts", "job_id", "to", "from"?, "reason"?, "record"?} — `record` carries the full
JobRecord dict on first sight (submission); unknown fields are ignored on replay (forward-compat).
"""
import json
import os
import threading
import time


class Journal:
    def __init__(self, path: str, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._jobs = {}  # job_id -> record dict (data-model.md §JobRecord)
        self._replay()

    # -- rebuild from disk -------------------------------------------------------------------
    def _replay(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn tail line (crash mid-write) — the fold is prefix-correct
                job_id = entry.get("job_id")
                if not job_id:
                    continue
                rec = self._jobs.get(job_id)
                if rec is None:
                    rec = dict(entry.get("record") or {"job_id": job_id})
                    self._jobs[job_id] = rec
                if entry.get("to"):
                    rec["state"] = entry["to"]
                if entry.get("reason") is not None:
                    rec["reason"] = entry["reason"]
                # Restore ALL non-control keys (018 T362): a terminal transition carries the kind's
                # result fields (mlflow_run_id/model/metrics/summary/…) so a completed job's record
                # is fully rebuilt after a restart — the gateway polls GET /train/{id} for the
                # registered version off exactly these. record/from/to/ts/job_id are control keys.
                for k, v in entry.items():
                    if k not in ("record", "from", "to", "ts", "job_id") and v is not None:
                        rec[k] = v

    def mark_interrupted(self, reason: str) -> int:
        """Startup pass (FR-173): every non-terminal job was killed with the previous agent —
        journal it as `interrupted` and return the count for the alert metric."""
        n = 0
        for rec in list(self._jobs.values()):
            if rec.get("state") in ("queued", "running"):
                self.transition(rec["job_id"], "interrupted", reason=reason,
                                ended_at=self.clock())
                n += 1
        return n

    # -- writes --------------------------------------------------------------------------------
    def submit(self, record: dict) -> None:
        """First sight of a job: journal the full record with its initial state.

        Durable-first (FR-189): the append is fsync'd before in-memory state advances, so a
        failed append never leaves memory reporting a transition that is not on disk."""
        with self._lock:
            self._append({"ts": self.clock(), "job_id": record["job_id"],
                          "to": record.get("state", "queued"), "record": record})
            self._jobs[record["job_id"]] = dict(record)

    def transition(self, job_id: str, to: str, *, reason=None, started_at=None,
                   ended_at=None, result=None, fields=None) -> None:
        """Journal a state change. `fields` (018 T362) carries arbitrary kind-specific outputs
        (mlflow_run_id/model/metrics/summary/best/…) that must survive a restart alongside the
        state — appended to the entry so `_replay` restores them; control keys can't be shadowed.

        Durable-first (019/US1, FR-189): the append is fsync'd BEFORE in-memory state advances, so a
        failed append never leaves memory reporting a transition that is not on disk."""
        with self._lock:
            existing = self._jobs.get(job_id)
            entry = {"ts": self.clock(), "job_id": job_id,
                     "from": (existing or {}).get("state"), "to": to}
            extra = dict(fields or {})
            for k, v in (("reason", reason), ("started_at", started_at),
                         ("ended_at", ended_at), ("result", result)):
                if v is not None:
                    extra[k] = v
            applied = {}
            for k, v in extra.items():
                if k in ("record", "from", "to", "ts", "job_id") or v is None:
                    continue  # never let a caller's field shadow a control key
                entry[k] = v
                applied[k] = v
            self._append(entry)  # durable-first (FR-189): raises before we touch memory
            rec = self._jobs.setdefault(job_id, {"job_id": job_id})
            rec["state"] = to
            for k, v in applied.items():
                rec[k] = v

    def _append(self, entry: dict) -> None:
        d = os.path.dirname(self.path) or "."
        existed = os.path.exists(self.path)
        os.makedirs(d, exist_ok=True)
        # Repair a torn tail before appending (FR-188): a crash mid-write can leave the file
        # ending without a newline — either a partial (unparseable) record or a complete record
        # whose newline was lost. Append mode seeks to raw EOF, so writing here would concatenate
        # the new record onto that tail, producing one line that replay then discards whole,
        # silently losing BOTH. Starting the new record on its own line isolates the tail: a
        # complete-but-unterminated record still replays; a partial one is skipped alone.
        prefix = ""
        try:
            if os.path.getsize(self.path) > 0:
                with open(self.path, "rb") as rf:
                    rf.seek(-1, os.SEEK_END)
                    if rf.read(1) != b"\n":
                        prefix = "\n"
        except OSError:
            pass
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(prefix + json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())  # a journalled transition survives the very next crash
        if not existed:
            self._fsync_dir(d)  # the new file's directory entry must be durable too

    @staticmethod
    def _fsync_dir(d: str) -> None:
        try:
            dfd = os.open(d, os.O_RDONLY)
        except OSError:
            return  # platforms without directory fds (e.g. Windows) — best-effort
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    # -- reads ----------------------------------------------------------------------------------
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
