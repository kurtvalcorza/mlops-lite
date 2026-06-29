"""Vision router (T022, US1): proxy image classification to the BentoML service.

The bento runs natively in WSL (CPU); the gateway forwards a base64 image as the multipart upload
BentoML expects. Same hybrid-split as LLM serving — `serve_up.ps1` injects the bento's IP.
"""
import base64
import hashlib
import os

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter
from pydantic import BaseModel

from .. import quality, registry

router = APIRouter()

BENTO_URL = os.getenv("BENTO_URL", "http://host.docker.internal:8092")
VISION_REQUESTS = Counter("gateway_vision_total", "Vision classify requests", ["status"])


async def _resolve_vision_version() -> tuple:
    """Best-effort (model_name, version) currently serving image-classification, for prediction logging
    — never raises, never blocks the response (None on any failure), mirroring /infer's resolve."""
    try:
        target = await run_in_threadpool(registry.resolve_serving_target, "image-classification")
        return (target["name"], target["version"]) if target else (None, None)
    except Exception:
        return (None, None)


def _top_label(data) -> object:
    """Top-1 predicted label from the bento response (handles {predictions:[{label}]} / {labels:[…]})."""
    preds = data.get("predictions") or data.get("labels") or []
    if preds and isinstance(preds[0], dict):
        return preds[0].get("label")
    return preds[0] if preds else data.get("label")


class ClassifyRequest(BaseModel):
    image_b64: str


@router.post("/vision/classify")
async def classify(req: ClassifyRequest):
    """Classify an image (base64 in, top-5 labels out) via the BentoML vision service."""
    try:
        raw = base64.b64decode(req.image_b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="image_b64 is not valid base64")
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(
                f"{BENTO_URL}/classify",
                files={"image": ("image.png", raw, "image/png")},
            )
        except httpx.HTTPError as e:
            VISION_REQUESTS.labels(status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"vision service unreachable at {BENTO_URL}: {e}")
    if r.status_code != 200:
        VISION_REQUESTS.labels(status="error").inc()
        raise HTTPException(status_code=502, detail=f"vision service error {r.status_code}: {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict) and data.get("busy"):
        # Expected GPU-lease contention (008 FR-067): the bento returns a structured busy marker
        # (200) rather than a 5xx (whose message BentoML masks). Surface the documented 409 GPU-busy
        # with the actionable hint, so stale-UI / direct-API clients get a clean refusal (Codex #6).
        VISION_REQUESTS.labels(status="busy").inc()
        raise HTTPException(status_code=409,
                            detail=data.get("detail", "GPU busy — free the GPU and retry"))
    VISION_REQUESTS.labels(status="ok").inc()
    # 013/FR-119: log the served classification off the request path (fire-and-forget, fail-open). The
    # input ref is the image's content hash (not the raw bytes) so capture-on logging stays light.
    name, version = await _resolve_vision_version()
    input_ref = "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    pid = quality.log_prediction(name, version, "image-classification", input_ref, _top_label(data))
    if isinstance(data, dict):
        data = {**data, "prediction_id": pid}
    return data


@router.get("/vision/health")
async def vision_health():
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{BENTO_URL}/readyz")
            return {"backend": "bentoml vision (native WSL, CPU)", "reachable": r.status_code == 200}
        except httpx.HTTPError:
            return {"backend": "bentoml vision (native WSL, CPU)", "reachable": False}
