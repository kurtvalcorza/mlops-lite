"""Model registry router (T020, US2): register, list/compare, and promote model versions.

Handlers are sync `def` on purpose — the MLflow client is blocking, so FastAPI runs each
in its threadpool rather than stalling the event loop.
"""
import os
import uuid
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import BaseModel

from .. import evaluation, quality, registry, shadow

router = APIRouter()

TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")

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


class ShadowReplayRequest(BaseModel):
    challenger: str                       # version to replay against the champion's logged traffic
    window_n: Optional[int] = None        # newest-N captured∩labeled pairs (default QUALITY_WINDOW_N)
    modality: Optional[str] = None        # auto-resolved from the @serving version's task tag if omitted


def _serving_modality(name: str, version: str) -> Optional[str]:
    """The registry `task` tag of `name@version` (the modality), best-effort."""
    try:
        for v in registry.list_versions(name):
            if str(v.get("version")) == str(version):
                return (v.get("tags") or {}).get("task")
    except Exception:
        return None
    return None


def _prepare_shadow(name: str, modality_hint: Optional[str], window_n: Optional[int]) -> dict:
    """Resolve the champion (@serving) + modality and run the 016 guards (blocking registry + store I/O,
    so call off the event loop). Returns the prepare() result augmented with the champion version,
    normalized modality, and a fresh shadow id."""
    served = registry.get_serving(name)
    if not served or not served.get("version"):
        raise shadow.ShadowError(f"{name} has no @serving champion to replay against")
    champion_version = served["version"]
    modality = modality_hint or _serving_modality(name, champion_version)
    if not modality:
        raise shadow.ShadowError(
            f"cannot determine the modality of {name}@{champion_version} — pass `modality` explicitly")
    prep = shadow.prepare(name, champion_version, modality, window_n=window_n)
    prep["champion_version"] = str(champion_version)
    prep["modality"] = quality.normalize_modality(modality)
    prep["shadow_id"] = uuid.uuid4().hex[:12]
    return prep


@router.post("/models/{name}/shadow-replay", status_code=202)
async def shadow_replay(name: str, req: ShadowReplayRequest):
    """016 US2: replay a challenger against the champion's logged production traffic (advisory only —
    never touches the promotion gate, SC-097). The gateway resolves the champion + guards the captured∩
    labeled window, then dispatches a trainer-side job that loads the challenger under the GPU lease (one
    model in VRAM) and scores it; the champion is read from logged predictions (no re-run). Poll
    `GET .../shadow-replay/{id}` for the verdict."""
    try:
        prep = await run_in_threadpool(_prepare_shadow, name, req.modality, req.window_n)
    except shadow.ShadowError as e:
        REGISTRY_OPS.labels(op="shadow", status="refused").inc()
        raise HTTPException(status_code=409, detail=str(e))
    except registry.RegistryError as e:
        raise HTTPException(status_code=502, detail=f"registry error: {e}")

    if prep["status"] != "ready":
        # no_corpus / inputs_not_captured / insufficient_data → a structured 409 with the guard fields at
        # top level (contracts/shadow-replay-endpoint.md), no job dispatched (FR-152).
        REGISTRY_OPS.labels(op="shadow", status=prep["status"]).inc()
        return JSONResponse(status_code=409,
                            content={k: v for k, v in prep.items() if k not in ("pairs", "shadow_id")})

    payload = {"shadow_id": prep["shadow_id"], "name": name, "challenger": req.challenger,
               "champion_version": prep["champion_version"], "modality": prep["modality"],
               "window_n": prep["n_pairs"]}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(f"{TRAINER_URL}/shadow-replay", json=payload)
        except httpx.HTTPError as e:
            REGISTRY_OPS.labels(op="shadow", status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"training daemon unreachable at {TRAINER_URL}: {e}")
    if r.status_code == 409:
        REGISTRY_OPS.labels(op="shadow", status="busy").inc()
        raise HTTPException(status_code=409, detail=r.json().get("error", "trainer busy"))
    if r.status_code not in (200, 202):
        REGISTRY_OPS.labels(op="shadow", status="error").inc()
        raise HTTPException(status_code=502, detail=f"trainer error {r.status_code}: {r.text[:200]}")
    REGISTRY_OPS.labels(op="shadow", status="queued").inc()
    return {"shadow_id": prep["shadow_id"], "status": "queued",
            "window_n": prep["n_pairs"], "champion_version": prep["champion_version"],
            "challenger": req.challenger, "modality": prep["modality"]}


@router.get("/models/{name}/shadow-replay/{shadow_id}")
async def get_shadow_replay(name: str, shadow_id: str):
    """The advisory verdict for a dispatched shadow-replay (016). Prefers the durable verdict persisted in
    the `results` bucket; falls back to the trainer's live job status while it is still running."""
    verdict = await run_in_threadpool(shadow.read_verdict, shadow_id)
    if verdict is not None:
        return verdict
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(f"{TRAINER_URL}/shadow-replay/{shadow_id}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"training daemon unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail=f"no shadow-replay {shadow_id}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"trainer error {r.status_code}: {r.text[:200]}")
    return r.json()
