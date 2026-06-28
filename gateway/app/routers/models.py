"""Model registry router (T020, US2): register, list/compare, and promote model versions.

Handlers are sync `def` on purpose — the MLflow client is blocking, so FastAPI runs each
in its threadpool rather than stalling the event loop.
"""
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

from .. import registry

router = APIRouter()

REGISTRY_OPS = Counter("gateway_registry_ops_total", "Model registry operations", ["op", "status"])


class RegisterRequest(BaseModel):
    name: str
    source: str
    run_id: Optional[str] = None
    tags: Optional[Dict[str, str]] = None


class PromoteRequest(BaseModel):
    version: str


@router.post("/models", status_code=201)
def register(req: RegisterRequest):
    """Register a new version of a model (creating the model on first use)."""
    try:
        mv = registry.register_model(req.name, req.source, req.run_id, req.tags)
    except registry.RegistryError as e:
        REGISTRY_OPS.labels(op="register", status="error").inc()
        raise HTTPException(status_code=502, detail=f"registry error: {e}")
    REGISTRY_OPS.labels(op="register", status="ok").inc()
    return mv


@router.get("/models")
def list_models():
    """All registered models and their current serving version."""
    try:
        return {"models": registry.list_models()}
    except registry.RegistryError as e:
        raise HTTPException(status_code=502, detail=f"registry error: {e}")


@router.get("/models/{name}")
def get_model(name: str):
    """All versions of one model (newest first) for comparison, plus the serving pointer."""
    try:
        versions = registry.list_versions(name)
    except registry.RegistryError as e:
        raise HTTPException(status_code=502, detail=f"registry error: {e}")
    if not versions:
        raise HTTPException(status_code=404, detail=f"no registered model named '{name}'")
    return {"name": name, "serving": registry.get_serving(name), "versions": versions}


@router.post("/models/{name}/promote")
def promote(name: str, req: PromoteRequest):
    """Promote a specific version to serving — US1 inference then resolves to it (FR-006)."""
    try:
        versions = registry.list_versions(name)
    except registry.RegistryError as e:
        raise HTTPException(status_code=502, detail=f"registry error: {e}")
    if not any(v["version"] == str(req.version) for v in versions):
        raise HTTPException(status_code=404, detail=f"{name!r} has no version {req.version}")
    try:
        res = registry.promote(name, req.version)
    except registry.RegistryError as e:
        REGISTRY_OPS.labels(op="promote", status="error").inc()
        raise HTTPException(status_code=502, detail=f"registry error: {e}")
    REGISTRY_OPS.labels(op="promote", status="ok").inc()
    return res
