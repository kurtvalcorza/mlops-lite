"""023 US4 (T510, FR-297..304) — ordered SQL migrations against a REAL Postgres.

Covers the contract matrix (contracts/schema-migrations.md): fresh apply, recognized legacy
adoption with row preservation, exact final shape, no-op repeat, concurrent runners, per-file
transaction rollback, checksum-mismatch refusal, and newer-schema refusal.

Needs a reachable Postgres and skips cleanly without one (the CI `backend` job provides a
postgres:17 service; locally any instance works):

    TEST_MIGRATIONS_ADMIN_DSN=postgresql://user:pass@host:port/db pytest tests/test_migrations.py

Each test runs in a FRESH database created off the admin DSN, so tests are order-independent and
never touch a real gateway database.
"""
import os
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from platformlib import migrations, store  # noqa: E402

ADMIN_DSN = os.getenv("TEST_MIGRATIONS_ADMIN_DSN",
                      "postgresql://migtest:migtest@127.0.0.1:55499/migtest")

psycopg = pytest.importorskip("psycopg")

try:
    _probe = psycopg.connect(ADMIN_DSN, connect_timeout=3)
    _probe.close()
except Exception:
    pytest.skip(f"no Postgres at TEST_MIGRATIONS_ADMIN_DSN ({ADMIN_DSN.rsplit('@', 1)[-1]}) — "
                f"the CI backend job provides one; locally start any instance",
                allow_module_level=True)

_SEQ = [0]


@pytest.fixture()
def fresh_db():
    """A brand-new database (dropped afterwards) + its DSN."""
    _SEQ[0] += 1
    name = f"mig_t{os.getpid()}_{_SEQ[0]}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield dsn
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()


def _conn(dsn, autocommit=True):
    return psycopg.connect(dsn, autocommit=autocommit)


def _tables(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'")
        return {r[0] for r in cur.fetchall()}


# --- fresh apply -------------------------------------------------------------------------------------

def test_fresh_apply_creates_full_schema_and_ledger(fresh_db):
    report = migrations.apply(dsn=fresh_db, applied_by="test", log=lambda *a: None)
    assert "001_baseline" in report["applied"]
    with _conn(fresh_db) as c:
        assert set(store.TABLES) <= _tables(c)                    # every store table exists
        assert migrations.verify_baseline_shape(c) == []          # exact recognized shape
        assert migrations.db_version(c) == report["db_version"] >= 1
        with c.cursor() as cur:                                   # meta seeded exactly once
            cur.execute("SELECT schema_version FROM meta")
            assert cur.fetchall() == [(1,)]
        migrations.check_compatible(c)                            # writers may proceed


def test_repeat_apply_is_a_noop(fresh_db):
    migrations.apply(dsn=fresh_db, applied_by="test", log=lambda *a: None)
    again = migrations.apply(dsn=fresh_db, applied_by="test", log=lambda *a: None)
    assert again["applied"] == []
    with _conn(fresh_db) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations WHERE version = 1")
        assert cur.fetchone() == (1,)                             # one ledger row, ever


# --- legacy adoption ---------------------------------------------------------------------------------

def _make_legacy(dsn):
    """Reproduce an installation created by the RETIRED duplicated bootstrap: the same objects,
    no ledger. The baseline file IS that DDL byte-for-byte, so executing it directly (without the
    runner) recreates the legacy state exactly."""
    baseline = [m for m in migrations.discover() if m.version == 1][0]
    with _conn(dsn) as c, c.cursor() as cur:
        cur.execute(baseline.sql)
        # populated state the adoption must preserve
        cur.execute("INSERT INTO predictions (prediction_id, model_name, version, modality, "
                    "served_at) VALUES ('p1', 'm', '1', 'text-generation', now())")
        cur.execute("INSERT INTO labels (prediction_id, label, submitted_at) "
                    "VALUES ('p1', '\"good\"'::jsonb, now())")
        cur.execute("INSERT INTO serving_llm (singleton, model_name, selected_at, selected_by) "
                    "VALUES (true, 'ops-bot', now(), 'operator')")


def test_legacy_database_is_adopted_with_rows_preserved(fresh_db):
    _make_legacy(fresh_db)
    with _conn(fresh_db) as c:
        assert migrations.db_version(c) == 0                      # tables, but no ledger
        with pytest.raises(migrations.MigrationError):            # unledgered ⇒ not compatible yet
            migrations.check_compatible(c)
    report = migrations.apply(dsn=fresh_db, applied_by="test", log=lambda *a: None)
    assert "001_baseline" in report["applied"]                    # stamped via idempotent apply
    with _conn(fresh_db) as c:
        migrations.check_compatible(c)
        assert migrations.verify_baseline_shape(c) == []
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM predictions")
            assert cur.fetchone() == (1,)                          # no fixture data lost
            cur.execute("SELECT model_name FROM serving_llm")
            assert cur.fetchone() == ("ops-bot",)
            cur.execute("SELECT count(*) FROM meta")
            assert cur.fetchone() == (1,)                          # seed did not duplicate


# --- concurrency -------------------------------------------------------------------------------------

def test_concurrent_runners_apply_exactly_once(fresh_db):
    results, errors = [], []

    def run():
        try:
            results.append(migrations.apply(dsn=fresh_db, applied_by="test",
                                            log=lambda *a: None))
        except Exception as e:  # noqa: BLE001 — collected for the assertion
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"concurrent apply raised: {errors}"
    applied_by = [r for r in results if r["applied"]]
    assert len(applied_by) == 1                                   # one winner; the rest no-op
    with _conn(fresh_db) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations WHERE version = 1")
        assert cur.fetchone() == (1,)


# --- failure / drift refusals ------------------------------------------------------------------------

@pytest.fixture()
def custom_dir(tmp_path):
    """A migrations dir whose 001 is the real baseline (copied bytes) — tests add later files."""
    baseline = [m for m in migrations.discover() if m.version == 1][0]
    (tmp_path / "001_baseline.sql").write_bytes(baseline.raw)
    return tmp_path


def test_failed_migration_rolls_back_schema_and_ledger(fresh_db, custom_dir):
    (custom_dir / "002_bad.sql").write_text(
        "CREATE TABLE half_done (id int PRIMARY KEY);\nSELECT 1/0;\n")
    with pytest.raises(migrations.MigrationError) as e:
        migrations.apply(dsn=fresh_db, directory=str(custom_dir), applied_by="test",
                         log=lambda *a: None)
    assert "002_bad" in str(e.value)
    with _conn(fresh_db) as c:
        assert migrations.db_version(c) == 1                      # 001 committed, 002 rolled back
        assert "half_done" not in _tables(c)                      # nothing partial survives


def test_checksum_mismatch_is_refused_never_repaired(fresh_db, custom_dir):
    (custom_dir / "002_add.sql").write_text("CREATE TABLE extra (id int PRIMARY KEY);\n")
    migrations.apply(dsn=fresh_db, directory=str(custom_dir), applied_by="test",
                     log=lambda *a: None)
    (custom_dir / "002_add.sql").write_text("CREATE TABLE extra (id bigint PRIMARY KEY);\n")
    with pytest.raises(migrations.MigrationError) as e:
        migrations.apply(dsn=fresh_db, directory=str(custom_dir), applied_by="test",
                         log=lambda *a: None)
    assert "checksum mismatch" in str(e.value)


def test_newer_database_is_refused_for_apply_and_writes(fresh_db, custom_dir):
    (custom_dir / "002_add.sql").write_text("CREATE TABLE extra (id int PRIMARY KEY);\n")
    migrations.apply(dsn=fresh_db, directory=str(custom_dir), applied_by="test",
                     log=lambda *a: None)
    # this binary only ships 001 in `custom_dir_v1` — the DB (at 002) is newer than it supports
    v1_only = custom_dir / "older_binary"
    v1_only.mkdir()
    baseline = [m for m in migrations.discover() if m.version == 1][0]
    (v1_only / "001_baseline.sql").write_bytes(baseline.raw)
    with pytest.raises(migrations.MigrationError) as e:
        migrations.apply(dsn=fresh_db, directory=str(v1_only), applied_by="test",
                         log=lambda *a: None)
    assert "newer" in str(e.value).lower()
    with _conn(fresh_db) as c:
        with pytest.raises(migrations.MigrationError):
            migrations.check_compatible(c, directory=str(v1_only))


def test_store_bootstrap_is_now_a_compatibility_check(fresh_db):
    """store.bootstrap() must never CREATE anything anymore — it verifies (FR-301)."""
    with _conn(fresh_db) as c:
        with pytest.raises(store.StoreError):                     # fresh DB, no ledger ⇒ refuse
            store.bootstrap(c)
        assert _tables(c) == set()                                # and it created NOTHING
    migrations.apply(dsn=fresh_db, applied_by="test", log=lambda *a: None)
    with _conn(fresh_db) as c:
        store.bootstrap(c)                                        # compatible ⇒ silent pass


def test_status_reports_versions_and_pending(fresh_db, custom_dir):
    (custom_dir / "002_add.sql").write_text("CREATE TABLE extra (id int PRIMARY KEY);\n")
    with _conn(fresh_db, autocommit=False) as c:
        st = migrations.status(c, directory=str(custom_dir))
    assert st["db_version"] == 0 and st["current"] == 2
    assert st["pending"] == ["001_baseline", "002_add"]
    migrations.apply(dsn=fresh_db, directory=str(custom_dir), applied_by="test",
                     log=lambda *a: None)
    with _conn(fresh_db, autocommit=False) as c:
        st = migrations.status(c, directory=str(custom_dir))
    assert st["db_version"] == 2 and st["pending"] == []
    assert [r["version"] for r in st["recorded"]] == [1, 2]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
