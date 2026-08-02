"""Tenancy repository — tenants, API keys, and quotas (026 T619/T620/T621, T630).

The broker's identity layer. Three rules are enforced here rather than at the call sites, because
every call site would otherwise have to remember them:

  * **A raw key is returned exactly once and never persisted.** `issue_key` returns the secret to
    its caller and stores only `sha256(raw)` plus a short non-secret `prefix` for display. There is
    no read path that can recover a raw key, so a leaked database is not a leaked key set.

    SHA-256 rather than a password KDF is deliberate: the secret is 256 bits of `secrets` entropy,
    not a human-chosen password, so there is no dictionary to slow down — and a KDF would put its
    work factor on *every authenticated request*, which is the one place this platform cannot
    afford it. The threat a KDF defends against (offline guessing of a low-entropy secret) does not
    exist for a random 256-bit token.

  * **Authentication is the conjunction of two statuses** (FR-002): a request authenticates iff the
    key is `active` AND its tenant is `active`. `resolve_key` returns None unless both hold, so
    revoking either one refuses the very next request without a second lookup the caller might skip.

  * **The system tenant is reserved and undeletable** (T621). Policy-triggered retrains are
    tenant-less work that must still be metered and lane-ordered; they run as this tenant instead of
    bypassing both. `delete_tenant` refuses it, and the schema allows at most one.
"""
import hashlib
import secrets
import uuid

from platformlib.storeimpl._base import StoreError, _epoch

#: The reserved system tenant's name (T621). Stable so the migration, the scheduler, and the admin
#: API all resolve the same row without threading an id through config.
SYSTEM_TENANT_NAME = "system"

#: Raw-key shape: `sk-` + 43 url-safe base64 chars (32 bytes of entropy). The `sk-` prefix makes a
#: leaked key greppable in logs and matches what OpenAI-compatible clients expect to be handed.
KEY_PREFIX = "sk-"
_KEY_BYTES = 32

#: How much of the raw key is retained as the non-secret display prefix. Long enough for an operator
#: to tell two of a tenant's keys apart in the console, far too short to brute-force the remainder.
_DISPLAY_PREFIX_CHARS = 11  # "sk-" + 8


def hash_key(raw: str) -> str:
    """The at-rest representation of a raw bearer key. See the module docstring for why SHA-256."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _display_prefix(raw: str) -> str:
    return raw[:_DISPLAY_PREFIX_CHARS]


def generate_key() -> str:
    """A fresh raw bearer key. Never stored — the caller shows it once and forgets it."""
    return KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)


# -- tenants ---------------------------------------------------------------------------------------

def create_tenant(conn, name: str, *, is_system: bool = False) -> dict:
    """Create a tenant. Raises StoreError when the name is taken (names are the operator's handle
    for a tenant, so a silent upsert would let two operators share one identity by accident)."""
    tenant_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name, status, is_system) VALUES (%s, %s, 'active', %s) "
            "ON CONFLICT (name) DO NOTHING RETURNING id, name, status, is_system, created_at",
            (tenant_id, name, is_system))
        row = cur.fetchone()
    if row is None:
        raise StoreError(f"tenant {name!r} already exists")
    return _tenant_row(row)


def ensure_system_tenant(conn) -> dict:
    """Idempotently materialize the reserved system tenant (T621). Called at gateway startup after
    migrations; safe to call on every boot."""
    existing = get_tenant_by_name(conn, SYSTEM_TENANT_NAME)
    if existing is not None:
        return existing
    try:
        return create_tenant(conn, SYSTEM_TENANT_NAME, is_system=True)
    except StoreError:
        # A concurrent booter won the insert — re-read rather than fail; both wanted the same row.
        existing = get_tenant_by_name(conn, SYSTEM_TENANT_NAME)
        if existing is None:
            raise
        return existing


def _tenant_row(row) -> dict:
    tenant_id, name, status, is_system, created_at = row
    return {"id": tenant_id, "name": name, "status": status,
            "is_system": bool(is_system), "created_at": _epoch(created_at)}


_TENANT_SELECT = "SELECT id, name, status, is_system, created_at FROM tenants"


def get_tenant(conn, tenant_id: str):
    with conn.cursor() as cur:
        cur.execute(_TENANT_SELECT + " WHERE id = %s", (tenant_id,))
        row = cur.fetchone()
    return _tenant_row(row) if row else None


def get_tenant_by_name(conn, name: str):
    with conn.cursor() as cur:
        cur.execute(_TENANT_SELECT + " WHERE name = %s", (name,))
        row = cur.fetchone()
    return _tenant_row(row) if row else None


def list_tenants(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(_TENANT_SELECT + " ORDER BY created_at")
        return [_tenant_row(r) for r in cur.fetchall()]


def set_tenant_status(conn, tenant_id: str, status: str) -> bool:
    """Enable/disable a tenant. Disabling denies every one of its keys on the next request without
    touching the keys themselves, so re-enabling restores the tenant's exact key set."""
    if status not in ("active", "disabled"):
        raise StoreError(f"invalid tenant status {status!r}")
    with conn.cursor() as cur:
        cur.execute("UPDATE tenants SET status = %s WHERE id = %s AND NOT is_system",
                    (status, tenant_id))
        return cur.rowcount > 0


def delete_tenant(conn, tenant_id: str) -> bool:
    """Delete a tenant and everything cascading from it. Refuses the system tenant (T621): the
    scheduler resolves it on every policy retrain, and deleting it would make those retrains
    unmeterable rather than merely unowned."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tenants WHERE id = %s AND NOT is_system", (tenant_id,))
        return cur.rowcount > 0


# -- api keys ---------------------------------------------------------------------------------------

def issue_key(conn, tenant_id: str) -> dict:
    """Mint a key for a tenant. Returns `{"id", "api_key", "prefix"}` — `api_key` is the RAW secret
    and is the only time it exists outside the caller's memory."""
    raw = generate_key()
    key_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (id, tenant_id, key_hash, prefix, status) "
            "VALUES (%s, %s, %s, %s, 'active')",
            (key_id, tenant_id, hash_key(raw), _display_prefix(raw)))
    return {"id": key_id, "tenant_id": tenant_id, "api_key": raw, "prefix": _display_prefix(raw)}


def rotate_key(conn, tenant_id: str) -> dict:
    """Issue a new key and revoke every prior key for the tenant, in one transaction. Rotation that
    left the old keys live would not be rotation — the compromised credential would still work."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET status = 'revoked', revoked_at = now() "
                "WHERE tenant_id = %s AND status = 'active'", (tenant_id,))
        return issue_key(conn, tenant_id)


def revoke_key(conn, key_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE api_keys SET status = 'revoked', revoked_at = now() "
                    "WHERE id = %s AND status = 'active'", (key_id,))
        return cur.rowcount > 0


def list_api_keys(conn, tenant_id: str) -> list:
    """Key metadata for display. `key_hash` is deliberately not selected — nothing outside
    `resolve_key` has a use for it, and a listing that carried it would put every tenant's
    verifier into console responses and logs."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, tenant_id, prefix, status, created_at, revoked_at FROM api_keys "
                    "WHERE tenant_id = %s ORDER BY created_at", (tenant_id,))
        return [{"id": r[0], "tenant_id": r[1], "prefix": r[2], "status": r[3],
                 "created_at": _epoch(r[4]), "revoked_at": _epoch(r[5])} for r in cur.fetchall()]


def resolve_key(conn, raw: str):
    """The auth hot path (T620): a raw bearer key -> the authenticated tenant, or None.

    Returns None unless the key is `active` AND its tenant is `active` — the join is what makes
    FR-002's two-status rule a single indivisible read rather than two the caller could get out of
    order. Also None for a malformed key, so callers never branch on shape.
    """
    if not raw:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id, t.name, t.status, t.is_system, t.created_at, k.id "
            "FROM api_keys k JOIN tenants t ON t.id = k.tenant_id "
            "WHERE k.key_hash = %s AND k.status = 'active' AND t.status = 'active'",
            (hash_key(raw),))
        row = cur.fetchone()
    if row is None:
        return None
    tenant = _tenant_row(row[:5])
    tenant["key_id"] = row[5]
    return tenant


# -- quotas -----------------------------------------------------------------------------------------

WINDOWS = ("daily", "weekly", "monthly")

#: `date_trunc` units per window. `weekly` truncates to the ISO week (Monday) — Postgres' own
#: definition, so the boundary an operator sees in SQL is the boundary the broker enforces.
_TRUNC = {"daily": "day", "weekly": "week", "monthly": "month"}


def set_quota(conn, tenant_id: str, window: str, budget_gpu_seconds: int) -> dict:
    if window not in WINDOWS:
        raise StoreError(f"invalid quota window {window!r} (expected one of {WINDOWS})")
    if budget_gpu_seconds < 0:
        raise StoreError("budget_gpu_seconds must be >= 0")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO quotas (tenant_id, quota_window, budget_gpu_seconds, updated_at) "
            "VALUES (%s, %s, %s, now()) ON CONFLICT (tenant_id) DO UPDATE SET "
            "quota_window = EXCLUDED.quota_window, budget_gpu_seconds = EXCLUDED.budget_gpu_seconds, "
            "updated_at = now()", (tenant_id, window, int(budget_gpu_seconds)))
    return {"tenant_id": tenant_id, "window": window,
            "budget_gpu_seconds": int(budget_gpu_seconds)}


def get_quota(conn, tenant_id: str):
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id, quota_window, budget_gpu_seconds, updated_at FROM quotas "
                    "WHERE tenant_id = %s", (tenant_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"tenant_id": row[0], "window": row[1], "budget_gpu_seconds": int(row[2]),
            "updated_at": _epoch(row[3])}


def window_start_sql(window: str) -> str:
    """The `date_trunc` expression for a window, as a SQL fragment.

    Returned as SQL rather than a Python-computed timestamp on purpose: the reserve step's
    overflow check has to evaluate the window boundary in the SAME statement that sums against it,
    or two requests racing across a boundary can each compute a different `now()` and authorize
    against different windows (T628/T678).
    """
    unit = _TRUNC.get(window)
    if unit is None:
        raise StoreError(f"invalid quota window {window!r}")
    return f"date_trunc('{unit}', now())"
