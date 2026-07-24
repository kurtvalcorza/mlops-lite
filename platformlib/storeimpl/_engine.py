"""Shared relational plumbing for the store repositories (024 US1, T571).

The connection factory + the migration-verify seam + the schema shape constants — the pieces every
repository and every store client needs, but that are NOT aggregate SQL. Lifted out of `store.py`
(024) so the facade becomes import-only, re-exported so `store.dsn`/`store.connect`/`store.bootstrap`/
`store.ensure_schema`/`store.SCHEMA_VERSION`/`store.TABLES` are unchanged. psycopg stays a lazy
per-call import (importing this module — and thus `store` — must never need the driver).
"""
import os

from platformlib.storeimpl._base import StoreError

#: Kept for backward compatibility with the pre-023 meta seed (001_baseline.sql seeds the same 1).
SCHEMA_VERSION = 1

#: The tables the schema must present — used by tests and shape checks. The DDL itself moved to
#: `platformlib/migrations/001_baseline.sql` (023 US4, FR-297): ordered migration files are the
#: ONLY schema authority now; the embedded full-schema DDL string and its init.sql mirror are gone.
TABLES = ("meta", "predictions", "labels", "capture_index", "jobs", "policies", "suggestions",
          "serving_llm")


def dsn() -> str:
    """The Postgres DSN for the `gateway` DB. `GATEWAY_DB_URL` (full DSN) wins; else assembled from
    the POSTGRES_* creds + GATEWAY_DB_HOST/PORT/NAME (defaults suit the gateway container)."""
    url = os.getenv("GATEWAY_DB_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "mlops")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("GATEWAY_DB_HOST", "postgres")
    port = os.getenv("GATEWAY_DB_PORT", "5432")
    name = os.getenv("GATEWAY_DB_NAME", "gateway")
    auth = f"{user}:{pw}@" if pw else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{name}"


def connect(dsn_str: str = None, *, autocommit: bool = True):
    """A psycopg connection to the gateway DB. psycopg is imported here (lazily) so importing this
    module never requires the driver. Raises StoreError on connection failure so callers can apply
    the fail-open (writes) / fail-loud (reads) posture without catching psycopg types."""
    try:
        import psycopg
    except ModuleNotFoundError as e:  # pragma: no cover - environment-dependent
        raise StoreError("psycopg is not installed — install psycopg[binary]") from e
    try:
        return psycopg.connect(dsn_str or dsn(), autocommit=autocommit)
    except Exception as e:  # noqa: BLE001 — normalize every driver/connection error to StoreError
        raise StoreError(f"cannot connect to the gateway DB: {e}") from e


def bootstrap(conn=None) -> None:
    """Verify the schema is COMPATIBLE (023 US4, FR-299/301) — this no longer applies DDL.

    Ordered migrations own evolution now: the GATEWAY applies them at startup
    (gateway/app/main.py); every other store client (host-agent journal, tools, ad-hoc writers)
    calls this before writing and fails closed on an unledgered/older/newer schema instead of
    racing its own `CREATE IF NOT EXISTS` against a live database. The name survives because
    every pre-023 client init already calls it at exactly the right moment."""
    from platformlib import migrations
    close = conn is None
    conn = conn or connect()
    try:
        try:
            migrations.check_compatible(conn)
        except migrations.MigrationError as e:
            raise StoreError(str(e)) from e
    finally:
        if close:
            conn.close()


def ensure_schema(dsn_str: str = None, applied_by: str = "tool") -> dict:
    """Apply pending migrations (the gateway-startup/CLI/test entry point — NOT for the agent's
    write path, which only checks). Returns the migrations report."""
    from platformlib import migrations
    try:
        return migrations.apply(dsn=dsn_str or dsn(), applied_by=applied_by, log=lambda *a: None)
    except migrations.MigrationError as e:
        raise StoreError(str(e)) from e
