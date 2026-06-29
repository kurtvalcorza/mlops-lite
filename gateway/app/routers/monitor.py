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

from .. import monitoring, quality

router = APIRouter()

TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")
RETRAINS = Counter("gateway_retrain_triggers_total", "Drift-triggered retrains", ["result"])
# 013 — distinguish WHICH breach signal fired a retrain (input-PSI vs output-quality), keeping each
# independently observable (FR-126); the original RETRAINS counter is left unchanged.
RETRAIN_SIGNAL = Counter("gateway_retrain_signal_total", "Retrains by breach signal",
                         ["signal", "result"])


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
            RETRAIN_SIGNAL.labels(signal="psi", result="launched").inc()
            # 013: the input-PSI breach is the *leading* signal — its fire decision is unchanged, but it
            # starts the shared cooldown so the (confirming) quality trigger won't immediately re-fire.
            quality.note_retrain()
        except Exception as e:
            RETRAINS.labels(result="failed").inc()
            RETRAIN_SIGNAL.labels(signal="psi", result="failed").inc()
            retrain = {"error": str(e)}  # surface, but the drift report still stands
    return {"report": report, "retrain": retrain}


@router.get("/monitor")
def monitor(limit: int = 20):
    """Recent drift reports (newest first)."""
    try:
        return {"reports": monitoring.latest_reports(limit)}
    except monitoring.MonitorError as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- 013: model-quality monitoring (ground truth) -------------------------------------------------

class LabelRequest(BaseModel):
    prediction_id: str
    label: object  # the ground-truth answer (class label / text / value) — modality-agnostic


class QualityCheck(BaseModel):
    model_name: Optional[str] = None       # registered model name (for the 011 baseline lookup)
    model_version: str                     # the version whose served predictions to score
    modality: str                          # task/modality key (reuses 011's metric registry)
    window_n: int = quality.WINDOW_N       # sliding count-based window (last N labeled pairs)
    drop_pct: float = quality.DROP_PCT     # breach: >X% below the 011 baseline
    baseline: Optional[float] = None       # override the auto-resolved 011 eval baseline
    retrain: Optional[RetrainSpec] = None  # when set, a breach fires this run (OR + cooldown)


@router.post("/monitor/labels")
async def submit_label(body: LabelRequest):
    """Attach a (usually delayed) ground-truth label to a logged prediction by id (013 US1, FR-120).

    Matches by `prediction_id` over the stored history, so a late label still counts; an unknown id or a
    duplicate label is reported cleanly (200 with a status) rather than overwriting served history."""
    try:
        res = await run_in_threadpool(quality.attach_label, body.prediction_id, body.label)
    except quality.QualityError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return res


@router.post("/monitor/quality/check")
async def quality_check(body: QualityCheck):
    """Compute windowed model-quality for a version and, on a baseline breach (+ retrain spec), fire the
    existing retrain — debounced by the shared OR+cooldown policy (013 US2/US3, FR-122/FR-125/FR-126)."""
    try:
        report = await run_in_threadpool(
            quality.compute_quality, body.model_name, body.model_version, body.modality,
            baseline=body.baseline, window_n=body.window_n, drop_pct=body.drop_pct)
    except quality.QualityError as e:
        raise HTTPException(status_code=400, detail=str(e))

    retrain = None
    if report["breach"] and body.retrain is not None:
        if quality.in_cooldown():
            retrain = {"skipped": "cooldown"}  # OR+cooldown debounce — a retrain fired too recently
        else:
            try:
                retrain = await run_in_threadpool(_launch_retrain, body.retrain)
                RETRAINS.labels(result="launched").inc()
                RETRAIN_SIGNAL.labels(signal="quality", result="launched").inc()
                quality.note_retrain()
            except Exception as e:  # fail-soft — the quality report still stands (FR-125)
                RETRAINS.labels(result="failed").inc()
                RETRAIN_SIGNAL.labels(signal="quality", result="failed").inc()
                retrain = {"error": str(e)}
    return {"report": report, "retrain": retrain}


@router.get("/monitor/quality")
def quality_reports(limit: int = 20):
    """Recent quality reports (newest first) — the output-side complement to GET /monitor."""
    try:
        return {"reports": quality.latest_quality_reports(limit)}
    except quality.QualityError as e:
        raise HTTPException(status_code=502, detail=str(e))
