"""Per-tenant bearer authentication for the broker surface (026 T620, T626, T627, T633).

This is a **second, independent** auth layer, not a replacement for `gateway/app/auth.py`. The
existing `X-API-Key` posture authenticates *the operator* to the platform's own lifecycle routes; the
broker authenticates *a tenant* to the LAN inference/jobs surface. They coexist because they answer
different questions — "may this caller drive the platform?" versus "whose GPU-seconds are these?" —
and collapsing them would give every LAN tenant the operator's key surface.

Three rules, each enforced here so no route can forget one:

  * **`Authorization: Bearer <key>` resolves to a tenant, or the request is refused** (FR-002). The
    resolution is a single indivisible read that checks the key's status and the tenant's status
    together; revoking either refuses the very next request.

  * **TLS is required whenever more than one tenant exists** (FR-002a, T626). Plaintext bearer keys
    on a shared LAN are not an acceptable posture once tenants are independent of each other: any
    peer on the segment can read a key off the wire and spend another tenant's quota. A plaintext
    request is **refused, never redirected** — a redirect would have already leaked the key in the
    request that triggered it.

  * **Cross-tenant reads are refused, not filtered** (T633). Every tenant-scoped route calls
    `require_own_tenant`, which compares the path's tenant against the authenticated one. Filtering
    a query by the caller's tenant is the more common pattern and is strictly worse here: it turns
    an authorization bug into an empty result set, which looks like "no data" rather than "you may
    not ask that", and it silently permits enumeration of which ids exist.
"""
import logging
import os

from fastapi import Header, Request

from platformlib import store as _store

from .broker import BrokerStoreError, conn, invalidate_conn, refuse

logger = logging.getLogger("gateway.tenancy")

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def tls_required() -> bool:
    """Whether the broker surface must refuse plaintext right now.

    Default ON. `BROKER_ALLOW_PLAINTEXT=1` is the documented single-tenant/dev escape hatch and logs
    a warning every time it lets a plaintext request through, so it cannot become the quiet default
    of a deployment nobody revisited.
    """
    return not _truthy(os.getenv("BROKER_ALLOW_PLAINTEXT"))


def _is_secure(request: Request) -> bool:
    """Whether this request reached us over TLS.

    `request.url.scheme` already reflects `X-Forwarded-Proto` when the app runs behind a proxy with
    `ProxyHeadersMiddleware`, which is how the compose deployment terminates TLS. A direct-to-uvicorn
    TLS deployment sets the scheme itself. Both are accepted; nothing else is.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def enforce_tls(request: Request) -> None:
    """Refuse a plaintext broker request (T626). Refused, never redirected — see the module docstring."""
    if not tls_required() or _is_secure(request):
        if not tls_required() and not _is_secure(request):
            logger.warning(
                "broker request served over PLAINTEXT (%s) — BROKER_ALLOW_PLAINTEXT is set; bearer "
                "keys are readable by any peer on this LAN segment", request.url.path)
        return
    raise refuse("unauthorized",
                 "TLS is required on the broker surface (FR-002a): bearer keys must not cross the "
                 "LAN in plaintext. Use https://; the request is refused rather than redirected so "
                 "the key is not sent again over http.")


def _bearer(authorization: str):
    """Extract the raw key from an `Authorization` header, or None. Case-insensitive scheme, because
    clients differ and rejecting `bearer` would be a needless interop failure."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def resolve_tenant(raw_key: str):
    """Raw key -> tenant dict, or None. Reconnects once on a dropped connection before giving up."""
    try:
        c = conn()
    except BrokerStoreError as e:
        raise refuse("store_unavailable", str(e))
    try:
        return _store.resolve_key(c, raw_key)
    except Exception as e:  # noqa: BLE001 — a dropped connection is the common case; retry once
        invalidate_conn(c)
        try:
            return _store.resolve_key(conn(), raw_key)
        except Exception:  # noqa: BLE001
            raise refuse("store_unavailable", f"tenant lookup failed: {e}")


async def require_tenant(request: Request, authorization: str = Header(None)) -> dict:
    """FastAPI dependency: the authenticated tenant for this request (T620).

    Also stamps `request.state.tenant` so the metering layer can attribute usage without re-resolving
    the key, and `request.state.op_id` — the idempotency key every reservation is written under
    (T627/T628). The op id is the client's `X-Request-Id` when it supplies one, so a client retrying
    a request it is unsure completed does not get charged twice.
    """
    enforce_tls(request)
    raw = _bearer(authorization)
    if raw is None:
        raise refuse("unauthorized", "missing bearer token: send `Authorization: Bearer <api-key>`")
    tenant = resolve_tenant(raw)
    if tenant is None:
        # Deliberately one message for every failure mode — unknown key, revoked key, disabled
        # tenant. Distinguishing them tells an attacker which of their guesses was a real key.
        raise refuse("unauthorized", "invalid or revoked API key")
    request.state.tenant = tenant
    request.state.op_id = _op_id(request)
    return tenant


def _op_id(request: Request) -> str:
    supplied = (request.headers.get("x-request-id") or "").strip()
    if supplied:
        return supplied[:200]
    import uuid
    return f"req-{uuid.uuid4()}"


def require_own_tenant(tenant: dict, path_tenant_id: str) -> None:
    """Refuse a cross-tenant access (T633). 404, not 403.

    403 confirms the id exists and merely belongs to someone else, which is an enumeration oracle:
    a caller probing ids learns the tenant list from the status codes alone. 404 says only "not
    something you can address", which is true from the caller's perspective and leaks nothing.
    """
    if path_tenant_id and tenant.get("id") != path_tenant_id:
        raise refuse("not_found", "no such tenant")


# -- owner (admin) authentication ---------------------------------------------------------------------

def _admin_keys() -> list:
    """Owner keys for the `/admin` surface. Falls back to the platform's existing operator key set,
    so an existing deployment gets a working admin surface without a second secret to provision."""
    raw = os.getenv("BROKER_ADMIN_KEYS") or os.getenv("GATEWAY_API_KEYS") or ""
    return [k for k in (s.strip() for s in raw.split(",")) if k]


async def require_owner(request: Request, authorization: str = Header(None),
                        x_api_key: str = Header(None)) -> dict:
    """FastAPI dependency for `/admin` (owner-only).

    Accepts the operator key by either header — `X-API-Key` is what the console BFF already injects,
    and `Authorization: Bearer` is what a CLI naturally sends. A *tenant* key never satisfies this:
    the admin surface mints keys and sets quotas, so a tenant reaching it could raise its own budget.
    """
    enforce_tls(request)
    import hmac

    presented = (x_api_key or "").strip() or _bearer(authorization) or ""
    keys = _admin_keys()
    if not keys:
        raise refuse("unauthorized",
                     "the broker admin surface is closed: set BROKER_ADMIN_KEYS (or GATEWAY_API_KEYS)")
    if not any(hmac.compare_digest(presented, k) for k in keys):
        raise refuse("unauthorized", "owner credentials required")
    request.state.owner = True
    return {"owner": True}
