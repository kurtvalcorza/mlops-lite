"""Monitoring router (T036, US5): expose drift reports and close the loop.

`POST /monitor/check` compares a reference vs a current dataset version; when drift crosses the
threshold and a `retrain` spec is supplied, it launches a fine-tune run on the training daemon —
drift -> retrain, the feedback loop (FR-010/FR-011). `GET /monitor` returns recent reports.
Drift compute is blocking (S3 + pure-Python PSI) → sync handlers run in FastAPI's threadpool.
"""
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter
from pydantic import BaseModel

from .. import monitoring

router = APIRouter()

TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")
RETRAINS = Counter("gateway_retrain_triggers_total", "Drift-triggered retrains", ["result"])


class DatasetRef(BaseModel):
    name: str
    version: str


class RetrainSpec(BaseModel):
    dataset_name: str
    dataset_version: str
    output_name: str
    steps: int = 10
    lora_r: int = 8


class DriftCheck(BaseModel):
    reference: DatasetRef
    current: DatasetRef
    threshold: float = monitoring.DEFAULT_THRESHOLD
    retrain: Optional[RetrainSpec] = None  # when set, breach launches this run


def _launch_retrain(spec: RetrainSpec) -> dict:
    """Fire a fine-tune run on the training daemon (the drift->retrain trigger)."""
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{TRAINER_URL}/train", json=spec.model_dump())
    if r.status_code not in (200, 202):
        raise RuntimeError(f"trainer returned {r.status_code}: {r.text[:200]}")
    return r.json()


@router.post("/monitor/check")
async def check(body: DriftCheck):
    """Run a drift check; on breach (and if a retrain spec is given) start a retraining run."""
    try:
        report = await run_in_threadpool(
            monitoring.compute_drift,
            body.reference.name, body.reference.version,
            body.current.name, body.current.version, body.threshold,
        )
    except monitoring.MonitorError as e:
        raise HTTPException(status_code=400, detail=str(e))

    retrain = None
    if report["dataset_drift"] and body.retrain is not None:
        try:
            retrain = await run_in_threadpool(_launch_retrain, body.retrain)
            RETRAINS.labels(result="launched").inc()
        except Exception as e:
            RETRAINS.labels(result="failed").inc()
            retrain = {"error": str(e)}  # surface, but the drift report still stands
    return {"report": report, "retrain": retrain}


@router.get("/monitor")
def monitor(limit: int = 20):
    """Recent drift reports (newest first)."""
    try:
        return {"reports": monitoring.latest_reports(limit)}
    except monitoring.MonitorError as e:
        raise HTTPException(status_code=502, detail=str(e))
