"""Policy + suggestion router (018 US3, T367/T370 — FR-179/183).

CRUD is whole-document PUT (single operator, no PATCH); validation is the shared contract and an
invalid declaration is a structured 400, never stored. Accepting a suggestion routes through the
EXISTING gated `registry.promote` — a suggestion never bypasses the gate (FR-183). Store outages
are 502; blocking calls run in the threadpool (house style).
"""
import json

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter

from .. import policies, registry

router = APIRouter()

POLICY_OPS = Counter("gateway_policy_ops_total", "Policy CRUD operations", ["op"])


def _handle(e: Exception):
    if isinstance(e, policies.PolicyError):
        try:
            detail = json.loads(str(e))  # the contract's structured {"errors": [...]} shape
        except ValueError:
            detail = str(e)
        raise HTTPException(status_code=400, detail=detail)
    if isinstance(e, policies.PolicyStoreError):
        raise HTTPException(status_code=502, detail=str(e))
    raise e


@router.get("/policies")
async def list_policies():
    try:
        return {"policies": await run_in_threadpool(policies.list_policies)}
    except Exception as e:
        _handle(e)


@router.get("/policies/{model_name}")
async def get_policy(model_name: str):
    doc = await run_in_threadpool(policies.get_policy, model_name)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no policy for {model_name!r}")
    return doc


@router.put("/policies/{model_name}")
async def put_policy(model_name: str, body: dict):
    POLICY_OPS.labels(op="put").inc()
    try:
        return await run_in_threadpool(policies.put_policy, model_name, body)
    except Exception as e:
        _handle(e)


@router.delete("/policies/{model_name}")
async def delete_policy(model_name: str):
    POLICY_OPS.labels(op="delete").inc()
    try:
        await run_in_threadpool(policies.delete_policy, model_name)
    except Exception as e:
        _handle(e)
    return {"deleted": model_name}


@router.get("/policies/{model_name}/status")
async def policy_status(model_name: str):
    try:
        status = await run_in_threadpool(policies.policy_status, model_name)
    except Exception as e:
        _handle(e)
    if status is None:
        raise HTTPException(status_code=404, detail=f"no policy for {model_name!r}")
    return status


# --- suggestions (FR-183) --------------------------------------------------------------------------

@router.get("/suggestions")
async def list_suggestions(state: str = None):
    try:
        return {"suggestions": await run_in_threadpool(policies.list_suggestions, state)}
    except Exception as e:
        _handle(e)


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_suggestion(suggestion_id: str):
    """Operator one-click: promote the suggested candidate through the gated choke-point, then
    mark the suggestion accepted. A gate `blocked` verdict still refuses (promoted=False) — the
    suggestion stays open so the operator can act deliberately (override lives on /promote)."""
    POLICY_OPS.labels(op="accept").inc()
    rec = await run_in_threadpool(policies.get_suggestion, suggestion_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown suggestion {suggestion_id!r}")
    if rec.get("state") != "open":
        raise HTTPException(status_code=409, detail=f"suggestion is already {rec.get('state')}")
    try:
        result = await run_in_threadpool(registry.promote, rec["model_name"],
                                         rec["candidate_version"])
    except registry.RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not result.get("promoted"):
        return {"promoted": False, "verdict": result.get("verdict"),
                "suggestion": rec, "detail": "gate blocked the promotion — suggestion stays open"}
    try:
        rec = await run_in_threadpool(policies.resolve_suggestion, suggestion_id, "accepted")
    except Exception as e:
        _handle(e)
    return {"promoted": True, "verdict": result.get("verdict"), "suggestion": rec}


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: str):
    POLICY_OPS.labels(op="dismiss").inc()
    try:
        rec = await run_in_threadpool(policies.resolve_suggestion, suggestion_id, "dismissed")
    except Exception as e:
        _handle(e)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown suggestion {suggestion_id!r}")
    return {"suggestion": rec}
