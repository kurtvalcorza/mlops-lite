"""Training-run router (T031, US4): launch + inspect LoRA fine-tune runs.

The gateway proxies to the native training daemon on the WSL GPU host (same hybrid-GPU split as
serving; `serve_up.ps1` injects its IP). The daemon enforces one-model-in-VRAM, so a launch may
come back 409 if the serving model is resident.
"""
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

router = APIRouter()

TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")
RUN_OPS = Counter("gateway_run_ops_total", "Training run operations", ["op", "status"])


class RunRequest(BaseModel):
    """A fine-tune launch (010 — multimodal). `modality` selects the flow the trainer dispatches
    (`llm` default for backward compatibility | `vision` | `embeddings` | `asr`); per-modality knobs
    are optional and default to each flow's conservative VRAM-fitting defaults when omitted (FR-098).
    `parent_version` chains a run from a prior registered version of `output_name` (US4, FR-096).
    Unknown extra fields are forwarded as-is via `model_dump()` — the trainer reads what it needs."""
    dataset_name: str
    dataset_version: str
    output_name: str
    modality: str = "llm"
    base_model: Optional[str] = None
    seed: int = 0
    parent_version: Optional[str] = None
    # LLM knobs (preserved defaults)
    steps: int = 10
    lora_r: int = 8
    # vision / embeddings / asr knobs (None → the flow's own default applies)
    epochs: Optional[int] = None
    lr: Optional[float] = None
    batch_size: Optional[int] = None
    unfreeze_epochs: Optional[int] = None
    warmup_ratio: Optional[float] = None
    grad_accum: Optional[int] = None
    quant: Optional[str] = None


@router.post("/runs", status_code=202)
async def launch_run(req: RunRequest):
    """Launch a fine-tune run on a pinned dataset version (async; poll GET /runs/{id}).

    Forwards only the set fields (`exclude_none`) so the trainer applies each flow's own per-modality
    defaults for anything the operator left blank, rather than overriding them with nulls."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(f"{TRAINER_URL}/train", json=req.model_dump(exclude_none=True))
        except httpx.HTTPError as e:
            RUN_OPS.labels(op="launch", status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"training daemon unreachable at {TRAINER_URL}: {e}")
    if r.status_code == 409:
        RUN_OPS.labels(op="launch", status="busy").inc()
        raise HTTPException(status_code=409, detail=r.json().get("error", "trainer busy"))
    if r.status_code == 507:
        # Live-VRAM admission refused the run (008.1 #11): forward it as the documented oversize
        # refusal with the trainer's hint, not a generic 502 (Codex PR#5 P2).
        RUN_OPS.labels(op="launch", status="rejected").inc()
        raise HTTPException(status_code=507, detail=r.json().get("error", "exceeds VRAM budget"))
    if r.status_code not in (200, 202):
        RUN_OPS.labels(op="launch", status="error").inc()
        raise HTTPException(status_code=502, detail=f"trainer error {r.status_code}: {r.text[:200]}")
    RUN_OPS.labels(op="launch", status="ok").inc()
    return r.json()


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Status + metrics for a run, including the registered model version once it completes."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(f"{TRAINER_URL}/train/{run_id}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"training daemon unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    return r.json()


@router.get("/training/health")
async def training_health():
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{TRAINER_URL}/health")
            return {"backend": "trainer (native WSL)", "reachable": True, **r.json()}
        except httpx.HTTPError:
            return {"backend": "trainer (native WSL)", "reachable": False}
