"""Tabular router (009 US4, T172 — FR-080/085): proxy tabular prediction to the BentoML service.

The tabular service runs natively in WSL on CPU, **off the GPU lease** — so, like /embed and unlike
/infer or /transcribe, there is no lease/busy handling: a /predict call succeeds even while a GPU
tenant holds the lease (always-available, CPU-only). Thin proxy, mirroring vision.py/embed.py;
up_all.ps1 injects the service IP via TABULAR_URL.
"""

import httpx
from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

from ..settings import TABULAR_URL, agent_headers

router = APIRouter()

TABULAR_REQUESTS = Counter("gateway_predict_total", "Tabular predict requests", ["status"])


class PredictRequest(BaseModel):
    rows: list[dict]


@router.post("/predict")
async def predict(req: PredictRequest):
    """One prediction per input row from the LightGBM joblib artifact (CPU, off-lease)."""
    if not req.rows:
        raise HTTPException(status_code=400, detail="rows must be a non-empty list of objects")
    async with httpx.AsyncClient(headers=agent_headers(), timeout=60) as client:
        try:
            r = await client.post(f"{TABULAR_URL}/predict", json={"rows": req.rows})
        except httpx.HTTPError as e:
            TABULAR_REQUESTS.labels(status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"tabular service unreachable at {TABULAR_URL}: {e}")
    if r.status_code != 200:
        TABULAR_REQUESTS.labels(status="error").inc()
        raise HTTPException(status_code=502, detail=f"tabular service error {r.status_code}: {r.text[:200]}")
    TABULAR_REQUESTS.labels(status="ok").inc()
    return r.json()


@router.get("/predict/health")
async def predict_health():
    async with httpx.AsyncClient(headers=agent_headers(), timeout=5) as client:
        try:
            r = await client.get(f"{TABULAR_URL}/readyz")
            return {"backend": "bentoml tabular (native WSL, CPU, off-lease)",
                    "reachable": r.status_code == 200}
        except httpx.HTTPError:
            return {"backend": "bentoml tabular (native WSL, CPU, off-lease)", "reachable": False}
