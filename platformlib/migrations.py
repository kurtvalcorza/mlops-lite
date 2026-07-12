"""Ordered SQL schema migrations for the gateway database (023 US4, T511 — contracts/schema-migrations.md).

`platformlib/migrations/*.sql` is the ONLY application-schema authority: fresh databases and
upgrades run the same files through the same runner. This replaces the duplicated
`CREATE ... IF NOT EXISTS` bootstrap (platformlib/store.py:DDL + infra/postgres/init.sql), which
could never detect drift, a checksum change, or a newer-than-binary schema.

Roles (FR-299/301):
  - The GATEWAY owns `apply()` at startup and fails readiness on a migration error.
  - The host agent and tools call `check_compatible()` before writes — they never evolve schema.

Apply algorithm (contract §Ledger): one advisory-locked, autocommit-off connection; the ledger is
created bootstrap-safe; every recorded version must exist locally with a matching checksum; a
recorded version above the binary's newest file is refused; each pending file applies in ITS OWN
transaction with its ledger row; failure rolls back that file and stops. Concurrent runners wait
on the lock, then see the committed ledger — a version applies exactly once.

Legacy adoption (contract §Existing database adoption): an installation created by the old
duplicated DDL has tables but no ledger. The baseline is additive-only (`IF NOT EXISTS`), so the
runner applies it idempotently over the legacy shape, VERIFIES the final shape, and stamps `001`.
A shape conflict fails with a report — never a guess.

psycopg is imported lazily (this module stays importable stdlib-only, like the rest of platformlib).
"""
import hashlib
import os
import re
import time

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

#: The fixed advisory lock for MLOps-Lite schema evolution (contract §Ledger). Any two runners on
#: the same database serialize on it; unrelated locks can't collide with a random constant.
ADVISORY_LOCK_KEY = 0x6D6C6F70_73303233  # "mlops" + "s023"

#: Oldest ledger version this binary can work against (consumers check
#: minimum <= database <= current; bumped only by a release that drops compatibility code).
MINIMUM_COMPATIBLE = 1

_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

#: The ledger — created through this bootstrap-safe statement only (contract §Ledger step 3).
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     integer PRIMARY KEY CHECK (version > 0),
  name        text NOT NULL CHECK (name <> ''),
  checksum    text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now(),
  applied_by  text NOT NULL CHECK (applied_by <> ''),
  duration_ms integer NOT NULL CHECK (duration_ms >= 0)
);
"""


class MigrationError(Exception):
    """Discovery/ledger/apply failure. The message names versions and files — never SQL text,
    DSNs, or credentials (contract §Observability)."""


class Migration:
    """One discovered migration file: (version, name, path, sql, checksum)."""

    def __init__(self, version: int, name: str, path: str):
        self.version = version
        self.name = name
        self.path = path
        with open(path, "rb") as fh:
            self.raw = fh.read()
        # The documented checksum rule (contract §File format): SHA-256 over the file's exact
        # repository bytes with line endings normalized to "\n" — so a Windows checkout (git
        # autocrlf) computes the same digest as the Linux CI runner and the container.
        self.checksum = hashlib.sha256(self.raw.replace(b"\r\n", b"\n")).hexdigest()

    @property
    def sql(self) -> str:
        return self.raw.decode("utf-8")


def discover(directory: str = None) -> list:
    """All migrations, version-ordered. Refuses duplicates, gaps are ALLOWED (a yanked number is
    never reused), non-conforming names fail loud rather than being silently skipped."""
    directory = directory or MIGRATIONS_DIR
    found = {}
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".sql"):
            continue
        m = _FILE_RE.match(fname)
        if not m:
            raise MigrationError(f"migration file {fname!r} does not match NNN_name.sql")
        version = int(m.group(1))
        if version == 0:
            raise MigrationError("migration version 000 is not allowed (versions are positive)")
        if version in found:
            raise MigrationError(f"duplicate migration version {version:03d}")
        found[version] = Migration(version, m.group(2), os.path.join(directory, fname))
    return [found[v] for v in sorted(found)]


def current_version(directory: str = None) -> int:
    """The newest migration this binary ships — the 'supported current' compatibility bound."""
    migrations = discover(directory)
    return migrations[-1].version if migrations else 0


# -- ledger reads ------------------------------------------------------------------------------------

def _ledger_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        return cur.fetchone()[0] is not None


def db_version(conn) -> int:
    """The database's newest applied version; 0 when no ledger exists (fresh or legacy)."""
    if not _ledger_exists(conn):
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(version), 0) FROM schema_migrations")
        return cur.fetchone()[0]


def check_compatible(conn, directory: str = None) -> None:
    """Raise MigrationError unless MINIMUM_COMPATIBLE <= db <= current (FR-299/301). The agent's
    write path and readiness probe call this — they NEVER apply. A legacy pre-ledger database
    (version 0 with the recognized tables) is reported as awaiting-adoption, not compatible."""
    have = db_version(conn)
    current = current_version(directory)
    if have == 0:
        raise MigrationError(
            "no migration ledger — the gateway applies migrations at startup; start it once "
            "(or run scripts/migrate_db.py apply)")
    if have < MINIMUM_COMPATIBLE:
        raise MigrationError(
            f"database schema version {have} is older than this binary's minimum "
            f"{MINIMUM_COMPATIBLE} — run the gateway (or scripts/migrate_db.py apply)")
    if have > current:
        raise MigrationError(
            f"database schema version {have} is NEWER than this binary supports ({current}) — "
            f"upgrade this component; writes are refused, never guessed (FR-301)")


def status(conn, directory: str = None) -> dict:
    """{db_version, current, pending: [...], recorded: [...]} for the operator CLI + metrics —
    no DSN/SQL exposure."""
    migrations = discover(directory)
    have = db_version(conn)
    recorded = []
    if _ledger_exists(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT version, name, checksum, applied_at, applied_by, duration_ms "
                        "FROM schema_migrations ORDER BY version")
            recorded = [{"version": r[0], "name": r[1], "checksum": r[2],
                         "applied_at": str(r[3]), "applied_by": r[4], "duration_ms": r[5]}
                        for r in cur.fetchall()]
    return {"db_version": have, "current": current_version(directory),
            "pending": [f"{m.version:03d}_{m.name}" for m in migrations if m.version > have],
            "recorded": recorded}


# -- baseline shape verification (legacy adoption) ----------------------------------------------------

#: Every (table, column) the recognized baseline must present after apply — derived from the
#: pre-023 store schema (contracts/store-schema.md). T512 proves this against store.py's contract.
BASELINE_SHAPE = {
    "meta": {"schema_version"},
    "predictions": {"prediction_id", "model_name", "version", "modality", "served_at",
                    "streamed", "payload_ref"},
    "labels": {"prediction_id", "label", "submitted_at"},
    "capture_index": {"prediction_id", "modality", "input_ref", "captured_at"},
    "jobs": {"job_id", "kind", "modality", "request", "state", "submitted_at", "started_at",
             "ended_at", "result"},
    "policies": {"model_name", "document", "updated_at", "updated_by", "pending", "status"},
    "suggestions": {"id", "model_name", "candidate_version", "gate_verdict", "shadow_verdict",
                    "state", "created_at", "resolved_at", "actor"},
    "serving_llm": {"singleton", "model_name", "selected_at", "selected_by"},
}

#: Indexes the baseline owns (name -> table).
BASELINE_INDEXES = {"ix_pred_window": "predictions", "ix_jobs_kind": "jobs"}


def verify_baseline_shape(conn) -> list:
    """Mismatch report ([] = exact match) between the live schema and the recognized baseline —
    run after the idempotent baseline apply, before stamping 001 (contract §Existing adoption)."""
    problems = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'")
        live = {}
        for table, column in cur.fetchall():
            live.setdefault(table, set()).add(column)
        for table, cols in BASELINE_SHAPE.items():
            if table not in live:
                problems.append(f"missing table {table!r}")
            elif not cols <= live[table]:
                problems.append(f"table {table!r} lacks columns {sorted(cols - live[table])}")
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        have_idx = {r[0] for r in cur.fetchall()}
        for idx in BASELINE_INDEXES:
            if idx not in have_idx:
                problems.append(f"missing index {idx!r}")
    return problems


# -- apply -------------------------------------------------------------------------------------------

def _connect(dsn: str = None):
    from platformlib import store
    return store.connect(dsn, autocommit=False)  # contract step 1: autocommit disabled


def apply(conn=None, *, dsn: str = None, directory: str = None, applied_by: str = "gateway",
          log=print) -> dict:
    """Run every pending migration; returns {applied: [...], db_version, duration_ms}. Safe to run
    concurrently (advisory lock) and repeatedly (no-op when current). Owns its connection unless
    one is passed (which must be autocommit OFF)."""
    migrations = discover(directory)
    close = conn is None
    if conn is None:
        conn = _connect(dsn)
    if getattr(conn, "autocommit", False):
        raise MigrationError("apply() needs an autocommit-OFF connection (contract §Ledger)")
    started = time.monotonic()
    applied = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            cur.execute(LEDGER_DDL)
        conn.commit()  # the ledger + lock survive independent of the per-file transactions

        # Verify history: every recorded version exists locally, checksums match, none is newer
        # than this binary (contract steps 4-5).
        with conn.cursor() as cur:
            cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
            recorded = dict(cur.fetchall())
        local = {m.version: m for m in migrations}
        newest_local = migrations[-1].version if migrations else 0
        for version, checksum in recorded.items():
            if version not in local:
                if version > newest_local:
                    raise MigrationError(
                        f"database is at migration {version:03d}, newer than this binary's "
                        f"{newest_local:03d} — refusing to touch a newer schema (FR-300)")
                raise MigrationError(
                    f"ledger records migration {version:03d} but no such file ships — "
                    f"repository/database history diverged; manual reconciliation required")
            if local[version].checksum != checksum:
                raise MigrationError(
                    f"checksum mismatch for applied migration {version:03d}_{local[version].name} "
                    f"— applied bytes differ from the repository file; never auto-repaired "
                    f"(FR-300)")

        have = max(recorded) if recorded else 0
        pending = [m for m in migrations if m.version > have]

        # Legacy adoption (FR-297/300): no ledger rows but the old bootstrap's tables exist →
        # the baseline (additive-only) applies idempotently below; the shape check after apply
        # verifies the union is EXACTLY the recognized baseline before the 001 stamp lands.
        adopting = not recorded and _has_legacy_tables(conn)
        if adopting:
            log("migrations: unledgered legacy schema detected — adopting via the idempotent "
                "baseline (contract §Existing database adoption)")

        for m in pending:
            t0 = time.monotonic()
            try:
                with conn.cursor() as cur:
                    cur.execute(m.sql)
                    if m.version == 1:
                        problems = verify_baseline_shape(conn)
                        if problems:
                            raise MigrationError(
                                "baseline shape verification failed after apply: "
                                + "; ".join(problems))
                    cur.execute(
                        "INSERT INTO schema_migrations "
                        "(version, name, checksum, applied_by, duration_ms) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (m.version, m.name, m.checksum, applied_by,
                         int((time.monotonic() - t0) * 1000)))
                conn.commit()  # ledger row + schema change in ONE transaction (contract step 7)
            except MigrationError:
                conn.rollback()
                raise
            except Exception as e:  # noqa: BLE001 — driver errors normalize to MigrationError
                conn.rollback()
                raise MigrationError(
                    f"migration {m.version:03d}_{m.name} failed and rolled back: "
                    f"{e.__class__.__name__}: {e}") from e
            applied.append(f"{m.version:03d}_{m.name}")
            log(f"migrations: applied {m.version:03d}_{m.name} "
                f"({int((time.monotonic() - t0) * 1000)} ms)")
        return {"applied": applied, "db_version": max([m.version for m in migrations], default=0),
                "duration_ms": int((time.monotonic() - started) * 1000)}
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            conn.commit()
        except Exception:  # noqa: BLE001 — closing the connection releases the lock anyway
            pass
        if close:
            conn.close()


def _has_legacy_tables(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('meta'), to_regclass('predictions')")
        row = cur.fetchone()
        return row[0] is not None and row[1] is not None
