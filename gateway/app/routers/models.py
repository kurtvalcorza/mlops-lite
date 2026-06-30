"""Model registry router (T020, US2): register, list/compare, and promote model versions.

Handlers are sync `def` on purpose — the MLflow client is blocking, so FastAPI runs each
in its threadpool rather than stalling the event loop.
"""
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter
from pydantic import BaseModel

from .. import evaluation, registry

router = APIRouter()

REGISTRY_OPS = Counter("gateway_registry_ops_total", "Model registry operations", ["op", "status"])


class RegisterRequest(BaseModel):
    name: str
    source: str
    run_id: Optional[str] = None
    tags: Optional[Dict[str, str]] = None


class PromoteRequest(BaseModel):
    version: str
    override: bool = False  # 011 FR-104: explicitly bypass a hard-gate block (promote-with-regression)


class EvaluateRequest(BaseModel):
    version: str
    benchmark: Optional[str] = None     # path under benchmarks/ (default: the modality's fixture)
    metric: Optional[str] = None        # override the modality's default primary metric


class CompareRequest(BaseModel):
    challenger: str                     # version to weigh against the current @serving champion
    benchmark: Optional[str] = None
    metric: Optional[str] = None


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
    """Promote a version to serving, **through the evaluation gate** (011 FR-105). The response carries
    the gate verdict and whether the alias moved (`promoted`): a default hard-gate block keeps the
    alias put with `promoted=false`; `pass`/`warn` (or an explicit `override`) moves it. US1 inference
    then resolves to the serving version (FR-006)."""
    try:
        versions = registry.list_versions(name)
    except registry.RegistryError as e:
        raise HTTPException(status_code=502, detail=f"registry error: {e}")
    if not any(v["version"] == str(req.version) for v in versions):
        raise HTTPException(status_code=404, detail=f"{name!r} has no version {req.version}")
    try:
        res = registry.promote(name, req.version, override=req.override)
    except registry.RegistryError as e:
        REGISTRY_OPS.labels(op="promote", status="error").inc()
        raise HTTPException(status_code=502, detail=f"registry error: {e}")
    REGISTRY_OPS.labels(op="promote", status="blocked" if not res.get("promoted", True) else "ok").inc()
    return res


@router.post("/models/{name}/evaluate")
async def evaluate(name: str, req: EvaluateRequest):
    """Evaluate a version, **guarded** (011 US1 + 015 US3, FR-143). The `@serving` version is scored via
    the serving path (one model in VRAM) and logged with benchmark provenance; a version that was scored
    at registration (015) returns its logged metric with no reload; a non-`@serving` version with no
    logged metric is **refused** (`409`) rather than silently scoring the resident model (the SC-068
    mislabel). Blocking (may load a model) → run it off the event loop."""
    try:
        res = await run_in_threadpool(
            evaluation.evaluate_guarded, name, req.version, req.benchmark, metric_name=req.metric)
    except evaluation.EvalGuardError as e:
        REGISTRY_OPS.labels(op="evaluate", status="guarded").inc()
        raise HTTPException(status_code=409, detail=f"eval guard: {e}")
    except evaluation.EvalError as e:
        REGISTRY_OPS.labels(op="evaluate", status="error").inc()
        raise HTTPException(status_code=400, detail=f"eval error: {e}")
    REGISTRY_OPS.labels(op="evaluate", status="ok").inc()
    return res


@router.post("/models/{name}/compare")
async def compare(name: str, req: CompareRequest):
    """Champion-challenger (011 US3 / 015 FR-142): declare a per-metric winner between the current
    `@serving` champion and the challenger by reading their **logged eval metrics** — no model reload.
    Since 015 every fine-tuned version is scored at registration, so both versions already carry a
    comparable metric and the comparison is a pure registry lookup (the degenerate "both legs hit the
    resident model" path the old live scorer suffered, SC-068, is gone). Loading no model trivially
    preserves the one-model-in-VRAM invariant (Principle II). A version with no logged metric → 400
    (evaluate/serve it first)."""
    try:
        res = await run_in_threadpool(
            evaluation.compare, name, req.challenger, req.benchmark, metric_name=req.metric)
    except evaluation.EvalError as e:
        REGISTRY_OPS.labels(op="compare", status="error").inc()
        raise HTTPException(status_code=400, detail=f"compare error: {e}")
    REGISTRY_OPS.labels(op="compare", status="ok").inc()
    return res
