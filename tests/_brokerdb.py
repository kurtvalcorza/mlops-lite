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
import os
import uuid

_DEFAULT_ADMIN_DSN = "postgresql://mlops:mlops@127.0.0.1:5432/postgres"


def admin_dsn() -> str:
    return os.getenv("BROKER_TEST_DSN") or os.getenv("GATEWAY_DB_URL") or _DEFAULT_ADMIN_DSN


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

        with psycopg.connect(self.admin, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{self.name}"')
        if self._apply:
            self.migrate()
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
        try:
            with psycopg.connect(self.admin, autocommit=True) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (self.name,))
                    cur.execute(f'DROP DATABASE IF EXISTS "{self.name}"')
        except Exception:  # noqa: BLE001 — a leaked scratch DB must never fail a passing suite
            pass
        return False


def requires_db():
    """`pytest.skip` unless a scratch Postgres is reachable — the suite's standard guard."""
    import pytest
    if not available():
        pytest.skip("no scratch Postgres (set BROKER_TEST_DSN, or start a local server) — "
                    "these suites assert real transactional behaviour and cannot use a fake")
