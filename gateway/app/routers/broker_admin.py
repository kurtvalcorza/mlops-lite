"""Owner-only admin surface for the broker (026 T624, T649, T671, T689 — contracts/admin-api.md).

Manage tenants, keys, and quotas; observe the queue, the resident set, and per-tenant usage. Every
route depends on `require_owner`, so a *tenant* key never reaches this surface: these routes mint
credentials and set budgets, and a tenant that could reach them could raise its own quota.

Two shapes here are dictated by review findings rather than taste:

  * **A raw key is in exactly one response, once.** Creation and rotation return it; nothing else
    ever can, because nothing else has it — the store keeps only a hash.

  * **`GET /admin/queue` exposes BOTH terms of BOTH VRAM bounds** (T689). The drill in quickstart.md
    asks an operator to assert invariant 1 (`accounted + reserved ≤ usable_capacity`) and invariant 2
    (each load ≤ `live_free − unmaterialized − safety_headroom`) by reading this endpoint alone. An
    earlier revision exposed only `vram_free_mb`, so neither bound was checkable from the documented
    surface and an operator had to infer values from agent logs — which is how a mid-transition
    observation gets mistaken for a violated invariant.
"""
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from platformlib import store as _store

from ..broker import BrokerStoreError, conn, refuse
from ..settings import AGENT_URL, agent_headers
from ..tenancy import require_owner

router = APIRouter(prefix="/admin", dependencies=[Depends(require_owner)])


def _conn():
    try:
        return conn()
    except BrokerStoreError as e:
        raise refuse("store_unavailable", str(e))


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class QuotaSet(BaseModel):
    window: str = Field(description="daily | weekly | monthly")
    budget_gpu_seconds: int = Field(ge=0)


# -- tenants + keys ----------------------------------------------------------------------------------

@router.post("/tenants", status_code=201)
def create_tenant(body: TenantCreate):
    """Create a tenant and its first key. The raw key is in this response and nowhere else."""
    c = _conn()
    try:
        tenant = _store.create_tenant(c, body.name.strip())
    except _store.StoreError as e:
        raise refuse("forbidden", str(e))
    issued = _store.issue_key(c, tenant["id"])
    return {"tenant_id": tenant["id"], "name": tenant["name"], "api_key": issued["api_key"],
            "key_id": issued["id"], "prefix": issued["prefix"]}


@router.get("/tenants")
def list_tenants():
    c = _conn()
    return {"tenants": [{**t, "keys": _store.list_api_keys(c, t["id"]),
                         "quota": _store.get_quota(c, t["id"])}
                        for t in _store.list_tenants(c)]}


@router.post("/tenants/{tenant_id}/keys", status_code=201)
def rotate_tenant_key(tenant_id: str, add: bool = False):
    """Rotate (default) or add a key.

    Rotation revokes every prior key; `?add=true` issues an additional one instead. Rotation is the
    default because it is what "my key leaked" needs, and an `add` that silently left the leaked key
    live would be the wrong default for the dangerous case.
    """
    c = _conn()
    if _store.get_tenant(c, tenant_id) is None:
        raise refuse("not_found", "no such tenant")
    issued = _store.issue_key(c, tenant_id) if add else _store.rotate_key(c, tenant_id)
    return {"tenant_id": tenant_id, "api_key": issued["api_key"], "key_id": issued["id"],
            "prefix": issued["prefix"], "rotated": not add}


@router.post("/tenants/{tenant_id}/revoke")
def revoke_tenant(tenant_id: str):
    """Disable a tenant: every key is denied on the next request; in-flight work ends gracefully.

    The keys themselves are untouched, so re-enabling restores the tenant's exact key set — a
    disable that destroyed keys would make "pause this tenant" and "expel this tenant" the same
    irreversible action.
    """
    c = _conn()
    if not _store.set_tenant_status(c, tenant_id, "disabled"):
        raise refuse("not_found", "no such tenant, or it is the reserved system tenant")
    return {"tenant_id": tenant_id, "status": "disabled"}


@router.post("/tenants/{tenant_id}/enable")
def enable_tenant(tenant_id: str):
    c = _conn()
    if not _store.set_tenant_status(c, tenant_id, "active"):
        raise refuse("not_found", "no such tenant, or it is the reserved system tenant")
    return {"tenant_id": tenant_id, "status": "active"}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str):
    c = _conn()
    if not _store.revoke_key(c, key_id):
        raise refuse("not_found", "no such active key")
    return {"key_id": key_id, "status": "revoked"}


# -- quotas ------------------------------------------------------------------------------------------

@router.put("/tenants/{tenant_id}/quota")
def set_quota(tenant_id: str, body: QuotaSet):
    c = _conn()
    if _store.get_tenant(c, tenant_id) is None:
        raise refuse("not_found", "no such tenant")
    try:
        return _store.set_quota(c, tenant_id, body.window, body.budget_gpu_seconds)
    except _store.StoreError as e:
        raise refuse("forbidden", str(e))


# -- usage --------------------------------------------------------------------------------------------

@router.get("/usage")
def usage(tenant: str = None, limit: int = 200):
    """Per-tenant window consumption plus recent ledger rows (FR-017, SC-004).

    `consumed` counts settled ledger rows AND outstanding reservations, which is the number the
    quota is actually enforced against — reporting only the settled half would show an operator a
    tenant well inside its budget at the moment the broker refuses it.
    """
    c = _conn()
    tenants = _store.list_tenants(c)
    if tenant:
        tenants = [t for t in tenants if t["id"] == tenant or t["name"] == tenant]
        if not tenants:
            raise refuse("not_found", "no such tenant")
    per_tenant = []
    for t in tenants:
        state = _store.consumption(c, t["id"])
        per_tenant.append({
            "tenant": t["name"], "tenant_id": t["id"],
            "window": state.get("window"), "window_start": state.get("window_start"),
            "budget_gpu_seconds": state.get("budget_gpu_seconds"),
            "consumed_gpu_seconds": state.get("consumed_gpu_seconds"),
            "settled_gpu_seconds": state.get("settled_gpu_seconds"),
            "outstanding_gpu_seconds": state.get("outstanding_gpu_seconds"),
            "remaining_gpu_seconds": state.get("remaining_gpu_seconds")})
    total = sum(p["consumed_gpu_seconds"] or 0.0 for p in per_tenant)
    return {"per_tenant": per_tenant, "total_gpu_seconds": total,
            "ledger": _store.list_ledger(c, tenant_id=tenants[0]["id"] if tenant else None,
                                         limit=limit),
            "reconciliation": _store.reconciliation(c)}


# -- queue + residency ---------------------------------------------------------------------------------

@router.get("/queue")
async def queue():
    """Live queue, resident set, and both VRAM bounds' terms (T689, SC-006/SC-009).

    Proxied from the host agent, which is the sole GPU-ordering authority — the gateway holds no
    admission state of its own, and synthesizing any here would create a second, lagging answer to
    a question with one authority.
    """
    async with httpx.AsyncClient(headers=agent_headers(), timeout=10) as client:
        try:
            r = await client.get(f"{AGENT_URL}/gpu/queue")
        except httpx.HTTPError as e:
            raise refuse("store_unavailable", f"host agent unreachable: {e}")
    if r.status_code != 200:
        raise refuse("store_unavailable", f"host agent error {r.status_code}")
    return r.json()


# -- owner override over the jobs lane (FR-025) ---------------------------------------------------------

class Reorder(BaseModel):
    position: int = Field(ge=1, description="1-based target position in the queued lane")


@router.post("/jobs/{job_id}/{action}")
async def job_override(job_id: str, action: str, body: Reorder = None):
    """Pin/pause/resume/reorder a QUEUED job (T649).

    A **running** job is never touched by any of these — that is FR-010/FR-023a, and it is enforced
    at the agent rather than here so the rule holds for every caller, not just this route.
    """
    if action not in ("pin", "pause", "resume", "reorder", "cancel"):
        raise refuse("forbidden", f"unknown override {action!r}")
    payload = {"action": action}
    if body is not None:
        payload["position"] = body.position
    async with httpx.AsyncClient(headers=agent_headers(), timeout=10) as client:
        try:
            r = await client.post(f"{AGENT_URL}/gpu/jobs/{job_id}/override", json=payload)
        except httpx.HTTPError as e:
            raise refuse("store_unavailable", f"host agent unreachable: {e}")
    if r.status_code == 404:
        raise refuse("not_found", "no such queued job")
    if r.status_code == 409:
        raise refuse("forbidden", "a running job is never preempted or reordered")
    if r.status_code != 200:
        raise refuse("store_unavailable", f"host agent error {r.status_code}")
    return r.json()
