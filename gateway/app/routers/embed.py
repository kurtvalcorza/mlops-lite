"""Embeddings router (009 US2, T163 — FR-078/085): proxy text embedding to the BentoML service.

The embeddings service runs natively in WSL on CPU, **off the GPU lease** — so unlike /infer and
/vision/classify there is no lease/busy handling here: an /embed call succeeds even while a GPU tenant
holds the lease (always-available, CPU-only). Thin proxy, mirroring vision.py; up_all.ps1 injects the
service IP via EMBED_URL.
"""
import os

import httpx
from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

router = APIRouter()

EMBED_URL = os.getenv("EMBED_URL", "http://host.docker.internal:8093")
EMBED_REQUESTS = Counter("gateway_embed_total", "Embedding requests", ["status"])


class EmbedRequest(BaseModel):
    texts: list[str]


@router.post("/embed")
async def embed(req: EmbedRequest):
    """Embed a batch of texts → a list of equal-dimension float vectors (CPU, off-lease)."""
    if not req.texts:
        raise HTTPException(status_code=400, detail="texts must be a non-empty list of strings")
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            r = await client.post(f"{EMBED_URL}/embed", json={"texts": req.texts})
        except httpx.HTTPError as e:
            EMBED_REQUESTS.labels(status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"embeddings service unreachable at {EMBED_URL}: {e}")
    if r.status_code != 200:
        EMBED_REQUESTS.labels(status="error").inc()
        raise HTTPException(status_code=502, detail=f"embeddings service error {r.status_code}: {r.text[:200]}")
    vectors = r.json()
    EMBED_REQUESTS.labels(status="ok").inc()
    dim = len(vectors[0]) if vectors and isinstance(vectors[0], list) else 0
    return {"model": os.getenv("EMBED_MODEL", "embed-minilm"), "device": "cpu",
            "count": len(vectors), "dim": dim, "vectors": vectors}


@router.get("/embed/health")
async def embed_health():
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{EMBED_URL}/readyz")
            return {"backend": "bentoml embeddings (native WSL, CPU, off-lease)",
                    "reachable": r.status_code == 200}
        except httpx.HTTPError:
            return {"backend": "bentoml embeddings (native WSL, CPU, off-lease)", "reachable": False}
