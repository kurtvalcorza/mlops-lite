"""Transcribe router (009 US3, T168 — FR-079/085): proxy ASR to the whisper.cpp daemon.

whisper.cpp runs as a native CUDA daemon on the WSL GPU host and **joins the single GPU lease** as a
tenant (like the LLM). So, like /infer and /vision/classify, a request can be refused on lease
contention: the ASR supervisor returns 409 when another GPU tenant holds the lease and 507 when live
VRAM can't admit the model — both surfaced here with their hints. Audio is carried as base64 in JSON
(mirroring /vision/classify); up_all.ps1 injects the daemon IP via ASR_URL.
"""
import asyncio
import base64
import os
import uuid

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter
from pydantic import BaseModel

from .. import quality, registry, swap

router = APIRouter()

ASR_URL = os.getenv("ASR_URL", "http://host.docker.internal:8095")
ASR_REQUESTS = Counter("gateway_transcribe_total", "Transcription requests", ["status"])
ASR_SERVING_MODEL = os.getenv("ASR_SERVING_MODEL")  # prefer this name when several asr models are promoted


def _resolve_asr_version() -> tuple:
    """Best-effort (model_name, version) currently serving asr, for prediction logging — never raises."""
    try:
        target = registry.resolve_serving_target("asr", ASR_SERVING_MODEL)
        return (target["name"], target["version"]) if target else (None, None)
    except Exception:
        return (None, None)


class TranscribeRequest(BaseModel):
    audio_b64: str
    filename: str = "audio.wav"
    language: str = "auto"
    preempt: bool = False  # 017: opt-in swap — evict a resident *serving* model first (default 008 refuse)


@router.post("/transcribe")
async def transcribe(req: TranscribeRequest):
    """Transcribe an audio clip (base64 in, text out) via the whisper.cpp GPU-lease daemon.

    017: with `preempt=true` and a different *serving* model resident, the gateway evicts it first so the
    ASR model can load (a **training** holder is never evicted → 409). Default is byte-for-byte 008."""
    try:
        base64.b64decode(req.audio_b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="audio_b64 is not valid base64")
    if req.preempt:
        await swap.preempt_or_409("asr")
    # Resolve the serving ASR version at request ARRIVAL, not in the post-serve detached task (016): if an
    # operator promotes a different asr version before that task runs, the old transcription would be
    # logged under the NEW version and its labels joined into the wrong champion window during shadow
    # replay. `_resolve_asr_version` is a quick registry lookup (never raises); a multi-second transcription
    # dominates, so this adds negligible latency while pinning the correct champion.
    asr_name, asr_version = await run_in_threadpool(_resolve_asr_version)
    async with httpx.AsyncClient(timeout=300) as client:
        try:
            r = await client.post(f"{ASR_URL}/transcribe", json={
                "audio_b64": req.audio_b64, "filename": req.filename, "language": req.language})
        except httpx.HTTPError as e:
            ASR_REQUESTS.labels(status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"ASR service unreachable at {ASR_URL}: {e}")
    if r.status_code == 409:
        # Lease held by another GPU tenant (one model in VRAM, Principle II) — pass the hint through.
        ASR_REQUESTS.labels(status="busy").inc()
        raise HTTPException(status_code=409, detail=r.json().get("error", "GPU busy — free the GPU and retry"))
    if r.status_code == 507:
        ASR_REQUESTS.labels(status="rejected").inc()
        raise HTTPException(status_code=507, detail=r.json().get("error", "model exceeds VRAM budget"))
    if r.status_code != 200:
        ASR_REQUESTS.labels(status="error").inc()
        raise HTTPException(status_code=502, detail=f"ASR service error {r.status_code}: {r.text[:200]}")
    ASR_REQUESTS.labels(status="ok").inc()
    data = r.json()
    # 013/016: log the served transcription off the request path (fire-and-forget, fail-open) so it can be
    # scored against a delayed label, and capture the recoverable AUDIO under the bounded opt-in policy so
    # the ASR champion can be shadow-replayed over real traffic (016 FR-146). The prediction id is returned
    # to the caller for label attachment; the registry resolve + store writes run off the response path.
    pid = (data.get("prediction_id") if isinstance(data, dict) else None) or uuid.uuid4().hex

    async def _log():
        text = data.get("text") if isinstance(data, dict) else None
        quality.log_prediction(asr_name, asr_version, "asr", req.filename, text, prediction_id=pid)
        quality.capture_input(pid, "asr", req.audio_b64, options={"language": req.language})

    try:
        asyncio.ensure_future(_log())
    except Exception:  # never let logging setup affect the served response (fail-open)
        pass
    if isinstance(data, dict):
        data = {**data, "prediction_id": pid}
    return data


@router.get("/transcribe/health")
async def transcribe_health():
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{ASR_URL}/health")
            return {"backend": "whisper.cpp (native WSL CUDA, GPU-lease tenant)",
                    "reachable": r.status_code == 200}
        except httpx.HTTPError:
            return {"backend": "whisper.cpp (native WSL CUDA, GPU-lease tenant)", "reachable": False}
