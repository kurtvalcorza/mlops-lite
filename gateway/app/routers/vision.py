"""Vision router (T022, US1): proxy image classification to the BentoML service.

The bento runs natively in WSL (CPU); the gateway forwards a base64 image as the multipart upload
BentoML expects. Same hybrid-split as LLM serving — `serve_up.ps1` injects the bento's IP.
"""
import base64
import os

import httpx
from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

router = APIRouter()

BENTO_URL = os.getenv("BENTO_URL", "http://host.docker.internal:8092")
VISION_REQUESTS = Counter("gateway_vision_total", "Vision classify requests", ["status"])


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
    VISION_REQUESTS.labels(status="ok").inc()
    return r.json()


@router.get("/vision/health")
async def vision_health():
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{BENTO_URL}/readyz")
            return {"backend": "bentoml vision (native WSL, CPU)", "reachable": r.status_code == 200}
        except httpx.HTTPError:
            return {"backend": "bentoml vision (native WSL, CPU)", "reachable": False}
