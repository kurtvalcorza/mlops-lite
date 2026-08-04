"""Scratch-Postgres harness for the broker suites (026 Phase 0/1).

The broker's correctness claims are almost all *database* claims — an atomic reserve that admits
exactly one of two racing requests, a settle that is idempotent under a mid-write kill, a window
binding that survives a boundary crossing. None of those can be demonstrated against an in-memory
fake: the fake would have to reimplement `SELECT … FOR UPDATE`, and a reimplementation that agrees
with Postgres is the thing under test. So these suites run against a real scratch database when one
is reachable, and skip cleanly when it is not.

Reachability is decided by `BROKER_TEST_DSN` (or the platform's ordinary `GATEWAY_DB_URL`). Each
harness call creates a uniquely-named database, applies the real migrations to it, and drops it
afterwards — so a run never depends on, or leaves behind, another run's state.
"""
import atexit
import os
import threading
import time
import uuid

_DEFAULT_ADMIN_DSN = "postgresql://mlops:mlops@127.0.0.1:5432/postgres"


def admin_dsn() -> str:
    """The Postgres to create scratch databases on.

    `TEST_MIGRATIONS_ADMIN_DSN` is included deliberately: CI already runs an ephemeral Postgres
    service under that name for `tests/test_migrations.py`, and without it every broker suite would
    *skip* on the one runner where they most need to run. A suite that silently skips in CI is
    indistinguishable from one that passes, which is the failure mode these guards exist to avoid —
    so the guard reaches for the database CI already provides rather than requiring a second one.
    """
    return (os.getenv("BROKER_TEST_DSN")
            or os.getenv("TEST_MIGRATIONS_ADMIN_DSN")
            or os.getenv("GATEWAY_DB_URL")
            or _DEFAULT_ADMIN_DSN)


def available() -> bool:
    """True when a Postgres we may create databases on is reachable."""
    try:
        import psycopg
    except ModuleNotFoundError:
        return False
    try:
        with psycopg.connect(admin_dsn(), autocommit=True, connect_timeout=3) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — any failure means "no scratch database", never a test error
        return False


def _db_dsn(admin: str, name: str) -> str:
    head, _, _ = admin.rpartition("/")
    return f"{head}/{name}"


#: A once-per-session **template** database with the migrations already applied. Every scratch
#: database is then `CREATE DATABASE … TEMPLATE <this>`, which Postgres serves as a file copy.
#:
#: Without it each of the ~90 database-backed tests re-ran the full migration set — advisory lock,
#: ledger reads, and every statement in three files — to produce a schema identical to the last
#: one's. That was the dominant cost of the suite on CI and bought nothing: the tests need a
#: *pristine* database, not a freshly-*migrated* one, and the template gives the same isolation
#: because each test still gets its own database.
_TEMPLATE = {"name": None}
_TEMPLATE_LOCK = threading.Lock()


def _template(admin: str) -> str:
    """The migrated template's name, creating it on first use."""
    import psycopg

    with _TEMPLATE_LOCK:
        if _TEMPLATE["name"] is not None:
            return _TEMPLATE["name"]
        name = f"broker_tmpl_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(admin, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}"')

        from platformlib import migrations
        migrations.apply(dsn=_db_dsn(admin, name), applied_by="test-template",
                         log=lambda *a: None)

        # `CREATE DATABASE … TEMPLATE` refuses while any session is connected to the template, and
        # `migrations.apply` opens its own. Nothing above holds one now, but say so explicitly:
        # a stray session here would fail *every* later scratch creation, far from its cause.
        with psycopg.connect(admin, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        _TEMPLATE["name"] = name
        atexit.register(_drop_template, admin, name)
        return name


def _drop_template(admin: str, name: str) -> None:
    """Drop the session's template at interpreter exit.

    Without this a developer's local Postgres accumulates one template per test run — small, but
    exactly the kind of residue this harness promises not to leave.
    """
    import psycopg

    try:
        with psycopg.connect(admin, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:  # noqa: BLE001 — at exit there is nobody left to tell
        pass


class ScratchDB:
    """A freshly-migrated database, dropped on exit.

    Usage::

        with ScratchDB() as db:
            with db.connect() as conn:
                ...
    """

    def __init__(self, apply_migrations: bool = True):
        self.name = f"broker_t_{uuid.uuid4().hex[:12]}"
        self.admin = admin_dsn()
        self.dsn = _db_dsn(self.admin, self.name)
        self._apply = apply_migrations
        self._conns = []

    def __enter__(self):
        import psycopg

        # Clone the migrated template rather than migrating again — same isolation (this is still a
        # database of its own), a fraction of the cost. `apply_migrations=False` skips the template
        # entirely, for the few tests that want a bare database.
        template = _template(self.admin) if self._apply else None
        clause = f' TEMPLATE "{template}"' if template else ""
        with psycopg.connect(self.admin, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{self.name}"{clause}')
        return self

    def migrate(self) -> dict:
        from platformlib import migrations
        return migrations.apply(dsn=self.dsn, applied_by="test", log=lambda *a: None)

    def rollback_broker(self) -> None:
        """Run the paired rollback for 003_broker (T618's revert leg)."""
        import pathlib
        sql = (pathlib.Path(__file__).resolve().parents[1]
               / "platformlib" / "migrations" / "rollback" / "003_broker.sql").read_text()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)

    def connect(self, autocommit: bool = True):
        import psycopg
        conn = psycopg.connect(self.dsn, autocommit=autocommit)
        self._conns.append(conn)
        return conn

    def tables(self) -> set:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            return {r[0] for r in cur.fetchall()}

    def __exit__(self, *exc):
        import psycopg

        for c in self._conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        # Drop the scratch database. A stray session on it makes DROP fail, so terminate first —
        # and retry once, because a backend takes a moment to actually go away after the signal.
        for attempt in range(2):
            try:
                with psycopg.connect(self.admin, autocommit=True) as c:
                    with c.cursor() as cur:
                        cur.execute(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = %s AND pid <> pg_backend_pid()", (self.name,))
                        cur.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
                return False
            except Exception as e:  # noqa: BLE001
                if attempt:
                    # Warn rather than raise: a leaked scratch database must never fail a passing
                    # suite, but it must not be *silent* either — silence is how a per-test leak
                    # grows until the server hits its connection or database limit.
                    import warnings
                    warnings.warn(f"scratch database {self.name} was not dropped: {e}",
                                  stacklevel=2)
                else:
                    time.sleep(0.2)
        return False


def requires_db():
    """`pytest.skip` unless a scratch Postgres is reachable — the suite's standard guard."""
    import pytest
    if not available():
        pytest.skip("no scratch Postgres (set BROKER_TEST_DSN, or start a local server) — "
                    "these suites assert real transactional behaviour and cannot use a fake")
