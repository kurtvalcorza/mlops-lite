"""Inference router (T015/T017/T021): POST /infer → on-demand LLM serving via the supervisor (US1).

T021 wires US1 to the registry: the response reports the registry version currently promoted to
`serving`, so an inference is traceable to a registered, promoted model version (FR-006).
"""
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter, Histogram
from pydantic import BaseModel

from .. import registry
from ..serving import SERVING_MODEL, ModelTooLargeError, ServingError, health, run_inference

router = APIRouter()

INFER_REQUESTS = Counter("gateway_infer_total", "Inference requests", ["status"])
INFER_LATENCY = Histogram("gateway_infer_latency_seconds", "Inference latency (excl. cold start)")
LOAD_LATENCY = Histogram("gateway_model_load_seconds", "Cold-start model load latency")


class InferRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.7


async def _resolve_serving_version() -> str | None:
    """Registry version promoted to serving for SERVING_MODEL, or None (best-effort)."""
    try:
        served = await run_in_threadpool(registry.get_serving, SERVING_MODEL)
        return served["version"] if served else None
    except Exception:
        return None


@router.post("/infer")
async def infer(req: InferRequest):
    """Submit a text prompt; returns the completion and metadata, including cold-start load time."""
    if not await health():
        INFER_REQUESTS.labels(status="unavailable").inc()
        raise HTTPException(status_code=503, detail="serving backend (supervisor) not reachable")
    try:
        result = await run_inference(req.prompt, req.max_tokens, req.temperature)
    except ModelTooLargeError as e:
        INFER_REQUESTS.labels(status="rejected").inc()
        raise HTTPException(status_code=400, detail=str(e))
    except ServingError as e:
        INFER_REQUESTS.labels(status="error").inc()
        raise HTTPException(status_code=502, detail=str(e))

    if result.get("load_ms"):
        LOAD_LATENCY.observe(result["load_ms"] / 1000.0)
    INFER_LATENCY.observe(result.get("infer_ms", 0) / 1000.0)
    INFER_REQUESTS.labels(status="ok").inc()
    return {
        "status": "completed",
        "registry_model": SERVING_MODEL,
        "registry_version": await _resolve_serving_version(),
        **result,
    }


@router.get("/serving/health")
async def serving_health():
    return {"backend": "llama-server (supervised)", "reachable": await health()}
