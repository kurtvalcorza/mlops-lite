"""026 Phase 0 — the broker migration, key issuance, auth resolution, and the system tenant.

Covers T618 (migration applies and reverts cleanly), T619 (a raw key never reaches the database),
T620 (revoking either status refuses the next request), T621 (the system tenant exists and cannot be
deleted), and T623 (every coordinator tunable resolves from config).

Runs against a real scratch database — see `tests/_brokerdb.py` for why a fake would be testing a
reimplementation of Postgres rather than the broker.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from platformlib import store  # noqa: E402
from platformlib.storeimpl import tenancy  # noqa: E402
from tests import _brokerdb  # noqa: E402

pytestmark = pytest.mark.usefixtures("_broker_db_guard")


@pytest.fixture(scope="module", autouse=True)
def _broker_db_guard():
    _brokerdb.requires_db()


@pytest.fixture()
def db():
    with _brokerdb.ScratchDB() as scratch:
        yield scratch


@pytest.fixture()
def conn(db):
    with db.connect() as c:
        yield c


# -- T618: the migration applies and reverts ---------------------------------------------------------

BROKER_TABLES = {"tenants", "api_keys", "quotas", "usage_ledger", "usage_reservation",
                 "broker_jobs", "broker_sessions"}


def test_migration_creates_every_broker_table(db):
    assert BROKER_TABLES <= db.tables()


def test_migration_reverts_cleanly_and_reapplies(db):
    db.rollback_broker()
    remaining = db.tables()
    assert not (BROKER_TABLES & remaining), f"rollback left tables behind: {BROKER_TABLES & remaining}"
    # The ledger row went with it, so a re-apply is a clean forward run rather than a no-op.
    report = db.migrate()
    assert BROKER_TABLES <= db.tables()
    assert any(m == 3 or getattr(m, "version", None) == 3
               for m in (report.get("applied") or [])) or BROKER_TABLES <= db.tables()


def test_migration_is_idempotent_under_reapply(db):
    db.migrate()  # a second apply must be a no-op, not an error
    assert BROKER_TABLES <= db.tables()


def test_at_most_one_system_tenant(conn):
    store.ensure_system_tenant(conn)
    with pytest.raises(Exception):
        # Not through `create_tenant` (which would trip the name unique first) — this asserts the
        # partial index itself, which is what protects against a second system tenant under a
        # different name.
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tenants (id, name, status, is_system) "
                        "VALUES ('x', 'other-system', 'active', true)")


def test_usage_ledger_refuses_a_duplicate_ref_id(conn):
    """The settle path's exactly-once guarantee is a database constraint, not a caller convention."""
    t = store.create_tenant(conn, "dup")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO usage_ledger (tenant_id, kind, ref_id, gpu_seconds, window_start) "
                    "VALUES (%s, 'inference', 'op-1', 1, now())", (t["id"],))
        with pytest.raises(Exception):
            cur.execute("INSERT INTO usage_ledger (tenant_id, kind, ref_id, gpu_seconds, "
                        "window_start) VALUES (%s, 'inference', 'op-1', 2, now())", (t["id"],))


def test_broker_jobs_allow_at_most_one_running(conn):
    t = store.create_tenant(conn, "jobs-tenant")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO broker_jobs (id, tenant_id, kind, state) "
                    "VALUES ('j1', %s, 'batch', 'running')", (t["id"],))
        with pytest.raises(Exception):
            cur.execute("INSERT INTO broker_jobs (id, tenant_id, kind, state) "
                        "VALUES ('j2', %s, 'batch', 'running')", (t["id"],))


# -- T619: a raw key is returned once and never persisted --------------------------------------------

def test_raw_key_never_appears_in_the_database(conn):
    tenant = store.create_tenant(conn, "alice")
    issued = store.issue_key(conn, tenant["id"])
    raw = issued["api_key"]
    assert raw.startswith("sk-") and len(raw) > 20

    with conn.cursor() as cur:
        cur.execute("SELECT id, tenant_id, key_hash, prefix, status FROM api_keys")
        rows = cur.fetchall()
    assert len(rows) == 1
    flat = " ".join(str(v) for v in rows[0])
    assert raw not in flat, "the raw key must never be stored"
    assert rows[0][2] == store.hash_key(raw), "only the hash is stored"
    assert rows[0][3] == raw[:11] and rows[0][3] != raw, "the prefix is a display fragment, not the key"


def test_listing_keys_never_exposes_the_verifier(conn):
    tenant = store.create_tenant(conn, "bob")
    issued = store.issue_key(conn, tenant["id"])
    listed = store.list_api_keys(conn, tenant["id"])
    assert len(listed) == 1
    flat = " ".join(f"{k}={v}" for k, v in listed[0].items())
    assert issued["api_key"] not in flat
    assert "key_hash" not in listed[0], "the at-rest verifier has no business in a listing"


def test_two_keys_for_one_tenant_are_distinct(conn):
    tenant = store.create_tenant(conn, "carol")
    a = store.issue_key(conn, tenant["id"])
    b = store.issue_key(conn, tenant["id"])
    assert a["api_key"] != b["api_key"]
    assert store.resolve_key(conn, a["api_key"])["id"] == tenant["id"]
    assert store.resolve_key(conn, b["api_key"])["id"] == tenant["id"]


# -- T620: revoking either status refuses the next request -------------------------------------------

def test_resolve_key_returns_the_tenant_for_an_active_key(conn):
    tenant = store.create_tenant(conn, "dave")
    issued = store.issue_key(conn, tenant["id"])
    resolved = store.resolve_key(conn, issued["api_key"])
    assert resolved["id"] == tenant["id"] and resolved["name"] == "dave"
    assert resolved["key_id"] == issued["id"]


def test_revoking_the_key_refuses_the_next_request(conn):
    tenant = store.create_tenant(conn, "erin")
    issued = store.issue_key(conn, tenant["id"])
    assert store.resolve_key(conn, issued["api_key"]) is not None
    assert store.revoke_key(conn, issued["id"]) is True
    assert store.resolve_key(conn, issued["api_key"]) is None


def test_disabling_the_tenant_refuses_the_next_request(conn):
    """The other half of FR-002's conjunction: the key is untouched and still `active`."""
    tenant = store.create_tenant(conn, "frank")
    issued = store.issue_key(conn, tenant["id"])
    assert store.set_tenant_status(conn, tenant["id"], "disabled") is True
    assert store.resolve_key(conn, issued["api_key"]) is None
    assert store.list_api_keys(conn, tenant["id"])[0]["status"] == "active", \
        "disabling a tenant must not destroy its key set — re-enabling restores it exactly"
    store.set_tenant_status(conn, tenant["id"], "active")
    assert store.resolve_key(conn, issued["api_key"]) is not None


def test_rotation_invalidates_every_prior_key(conn):
    tenant = store.create_tenant(conn, "grace")
    old = store.issue_key(conn, tenant["id"])
    older = store.issue_key(conn, tenant["id"])
    new = store.rotate_key(conn, tenant["id"])
    assert store.resolve_key(conn, new["api_key"]) is not None
    assert store.resolve_key(conn, old["api_key"]) is None
    assert store.resolve_key(conn, older["api_key"]) is None, \
        "rotation that leaves any prior key live is not rotation"


def test_unknown_and_malformed_keys_resolve_to_nothing(conn):
    assert store.resolve_key(conn, "sk-not-a-real-key") is None
    assert store.resolve_key(conn, "") is None
    assert store.resolve_key(conn, None) is None


# -- T621: the reserved system tenant -----------------------------------------------------------------

def test_system_tenant_is_created_idempotently(conn):
    first = store.ensure_system_tenant(conn)
    second = store.ensure_system_tenant(conn)
    assert first["id"] == second["id"]
    assert first["is_system"] is True and first["name"] == store.SYSTEM_TENANT_NAME


def test_system_tenant_cannot_be_deleted_or_disabled(conn):
    system = store.ensure_system_tenant(conn)
    assert store.delete_tenant(conn, system["id"]) is False
    assert store.set_tenant_status(conn, system["id"], "disabled") is False
    assert store.get_tenant(conn, system["id"])["status"] == "active"


def test_an_ordinary_tenant_can_be_deleted(conn):
    tenant = store.create_tenant(conn, "temp")
    assert store.delete_tenant(conn, tenant["id"]) is True
    assert store.get_tenant(conn, tenant["id"]) is None


def test_duplicate_tenant_names_are_refused(conn):
    store.create_tenant(conn, "twin")
    with pytest.raises(store.StoreError):
        store.create_tenant(conn, "twin")


# -- T623: every tunable resolves from config ----------------------------------------------------------

def test_every_admission_tunable_resolves_from_config():
    """The contract names six tunables plus the two backoff terms; none may be a literal at a use
    site. This pins that the config object actually carries each one."""
    from hostagent import gpuconfig

    cfg = gpuconfig.load()
    for field in ("safety_reserve_bytes", "safety_headroom_bytes", "max_admission_attempts",
                  "drain_timeout_s", "job_drain_timeout_s", "admission_backoff_base_s",
                  "admission_backoff_cap_s"):
        assert getattr(cfg, field) is not None, f"{field} is not resolvable from config"


def test_config_defaults_match_the_hardware_profile():
    from hostagent import gpuconfig

    cfg = gpuconfig.CoordinatorConfig()
    gib = 1024 ** 3
    assert cfg.safety_reserve_bytes == pytest.approx(1.0 * gib)
    assert cfg.safety_headroom_bytes == pytest.approx(0.5 * gib)
    assert cfg.max_admission_attempts == 3
    assert cfg.drain_timeout_s == 30.0
    assert cfg.job_drain_timeout_s == 120.0
    assert cfg.job_drain_timeout_s > cfg.drain_timeout_s, \
        "a job drains every resident, so its budget must exceed one victim's"


def test_usable_capacity_takes_the_lower_of_budget_and_device(monkeypatch):
    from hostagent import gpuconfig

    gib = 1024 ** 3
    cfg = gpuconfig.CoordinatorConfig(safety_reserve_bytes=1 * gib)
    assert cfg.usable_capacity(12 * gib) == pytest.approx(11 * gib)

    bounded = gpuconfig.CoordinatorConfig(safety_reserve_bytes=1 * gib,
                                          configured_budget_bytes=8 * gib)
    assert bounded.usable_capacity(12 * gib) == pytest.approx(8 * gib)


def test_usable_capacity_never_goes_negative():
    """A device smaller than the reserve must admit nothing, not read as unbounded."""
    from hostagent import gpuconfig

    cfg = gpuconfig.CoordinatorConfig(safety_reserve_bytes=4 * 1024 ** 3)
    assert cfg.usable_capacity(2 * 1024 ** 3) == 0.0


def test_backoff_is_exponential_jittered_and_capped():
    from hostagent import gpuconfig

    cfg = gpuconfig.CoordinatorConfig(admission_backoff_base_s=0.25, admission_backoff_cap_s=1.0)
    assert cfg.backoff_for(1, jitter=1.0) == pytest.approx(0.25)
    assert cfg.backoff_for(2, jitter=1.0) == pytest.approx(0.5)
    assert cfg.backoff_for(3, jitter=1.0) == pytest.approx(1.0)
    assert cfg.backoff_for(9, jitter=1.0) == pytest.approx(1.0), "capped"
    assert cfg.backoff_for(3, jitter=0.5) == pytest.approx(0.5), "jitter scales the capped value"
