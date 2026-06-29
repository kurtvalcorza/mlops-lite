"""Batch-inference router (014 US1): proxy batch jobs to the native daemon.

The Docker gateway has no GPU, so a batch job runs as an **ephemeral Prefect flow on the native daemon**
(`training/flows/batch_infer.py`) where serving lives — this router is the launch/status proxy, mirroring
the `/runs` ↔ trainer `/train` split. `POST /batch` submits, `GET /batch/{id}` polls.
"""
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

router = APIRouter()

TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")
BATCH_OPS = Counter("gateway_batch_ops_total", "Batch-inference operations", ["op", "status"])


class BatchRequest(BaseModel):
    """A batch-inference launch: score `dataset_name@dataset_version` against `model` (the `@serving`
    model or a registered version). `modality` selects the serving path; GPU modalities go through the
    one-model-in-VRAM lease, tabular scores off-lease (FR-130)."""
    dataset_name: str
    dataset_version: str
    model: str
    modality: str = "llm"
    registry_version: Optional[str] = None
    abort_threshold: float = 0.5            # abort if > this fraction of rows fail (FR-129)


@router.post("/batch", status_code=202)
async def launch_batch(req: BatchRequest):
    """Launch a batch-inference job (async; poll GET /batch/{id}). Proxies to the native daemon, which
    scores every row through the existing serving tenant (the one-model-in-VRAM lease for GPU modalities;
    off-lease for tabular) and writes a content-addressed result to MinIO. The daemon's `_active` gate
    serializes the batch against train/study; it does not acquire its own lease — serving owns VRAM."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(f"{TRAINER_URL}/batch", json=req.model_dump(exclude_none=True))
        except httpx.HTTPError as e:
            BATCH_OPS.labels(op="launch", status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"training daemon unreachable at {TRAINER_URL}: {e}")
    if r.status_code == 409:
        BATCH_OPS.labels(op="launch", status="busy").inc()
        raise HTTPException(status_code=409, detail=r.json().get("error", "trainer busy"))
    if r.status_code not in (200, 202):
        BATCH_OPS.labels(op="launch", status="error").inc()
        raise HTTPException(status_code=502, detail=f"trainer error {r.status_code}: {r.text[:200]}")
    BATCH_OPS.labels(op="launch", status="ok").inc()
    return r.json()


@router.get("/batch/{batch_id}")
async def get_batch(batch_id: str):
    """Status + result for a batch job (counts in/out/failed + the content-addressed result URI)."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(f"{TRAINER_URL}/batch/{batch_id}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"training daemon unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail=f"no batch {batch_id}")
    return r.json()
