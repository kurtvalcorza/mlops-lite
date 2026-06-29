"""Inference router (T015/T017/T021): POST /infer → on-demand LLM serving via the supervisor (US1).

T021 wires US1 to the registry: the response reports the registry version currently promoted to
`serving`, so an inference is traceable to a registered, promoted model version (FR-006).
"""
import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter, Histogram
from pydantic import BaseModel

from .. import quality, registry, tracing
from ..serving import (
    SERVING_MODEL,
    ModelTooLargeError,
    ServingError,
    gpu_state,
    health,
    run_inference,
)

router = APIRouter()

INFER_REQUESTS = Counter("gateway_infer_total", "Inference requests", ["status"])
INFER_LATENCY = Histogram("gateway_infer_latency_seconds", "Inference latency (excl. cold start)")
LOAD_LATENCY = Histogram("gateway_model_load_seconds", "Cold-start model load latency")


class InferRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.7


async def _resolve_serving_version() -> str | None:
    """Registry version currently serving the LLM (FR-006/FR-075).

    /infer always proxies to the single llama supervisor configured by SERVING_MODEL, so SERVING_MODEL's
    `@serving` alias is authoritative for the reported version — resolve it FIRST. This preserves pre-009
    /infer behavior exactly (T156): the promoted version is reported even when it predates 009's `task`
    tags, and a *different* promoted text-generation model can never be mis-reported as the served one.
    (The earlier task-first resolve mis-fired when SERVING_MODEL's `@serving` version was untagged but a
    text-generation tag existed elsewhere — `resolve_serving_target` then returned that other model's
    version, since a candidate must be BOTH `task`-tagged AND its model's `@serving` version.)

    The task-based resolve remains only as a fallback for the edge where SERVING_MODEL has nothing
    promoted to `@serving`. Best-effort — any failure yields None (the response just omits the version).
    """
    try:
        served = await run_in_threadpool(registry.get_serving, SERVING_MODEL)
        if served:
            return served["version"]
        target = await run_in_threadpool(
            registry.resolve_serving_target, "text-generation", SERVING_MODEL)
        return target["version"] if target else None
    except Exception:
        return None


@router.post("/infer")
async def infer(req: InferRequest):
    """Submit a text prompt; returns the completion and metadata, including cold-start load time.

    Emits one fire-and-forget MLflow trace per request (006/FR-049) — including error outcomes — from
    a `finally`, off the request path. The span is at the router, naturally outside the GPU lock.
    """
    start_ns = time.time_ns()
    result = None
    registry_version = None
    # Pessimistic default: any UNEXPECTED failure (e.g. a malformed supervisor response) keeps the trace
    # errored — only the known-good path flips it to OK just before returning (006 Codex review).
    trace_status = "ERROR"
    outcome = "error"
    try:
        if not await health():
            INFER_REQUESTS.labels(status="unavailable").inc()
            trace_status, outcome = "ERROR", "unavailable"
            raise HTTPException(status_code=503, detail="serving backend (supervisor) not reachable")
        try:
            result = await run_inference(req.prompt, req.max_tokens, req.temperature)
        except ModelTooLargeError as e:
            INFER_REQUESTS.labels(status="rejected").inc()
            trace_status, outcome = "ERROR", "rejected"
            raise HTTPException(status_code=400, detail=str(e))
        except ServingError as e:
            INFER_REQUESTS.labels(status="error").inc()
            trace_status, outcome = "ERROR", "error"
            raise HTTPException(status_code=502, detail=str(e))

        if result.get("load_ms"):
            LOAD_LATENCY.observe(result["load_ms"] / 1000.0)
        INFER_LATENCY.observe(result.get("infer_ms", 0) / 1000.0)
        INFER_REQUESTS.labels(status="ok").inc()
        registry_version = await _resolve_serving_version()
        trace_status, outcome = "OK", "completed"  # success is known — flip the pessimistic default
        # 013/FR-119: log the served prediction off the request path (fire-and-forget, fail-open) so it
        # can be scored later against a delayed label. Returns a synchronous id regardless of store state.
        prediction_id = quality.log_prediction(
            SERVING_MODEL, registry_version, "text-generation", req.prompt, result.get("text"))
        return {
            "status": "completed",
            "registry_model": SERVING_MODEL,
            "registry_version": registry_version,
            "prediction_id": prediction_id,
            **result,
        }
    finally:
        attrs = {
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "model": SERVING_MODEL,
            "status": outcome,
        }
        if result:
            for k in ("load_ms", "infer_ms", "usage"):
                if result.get(k) is not None:
                    attrs[k] = result[k]
        if registry_version is not None:
            attrs["registry_version"] = registry_version
        tracing.emit(
            name="infer",
            inputs={"prompt": req.prompt},
            outputs={"output": result.get("text")} if result else None,
            attributes=attrs,
            start_ns=start_ns,
            end_ns=time.time_ns(),
            status=trace_status,
        )


@router.get("/serving/health")
async def serving_health():
    return {"backend": "llama-server (supervised)", "reachable": await health()}


@router.get("/serving/state")
async def serving_state():
    """GPU/lease state for the Infer status line (008 US3, FR-068): {holder, resident,
    serving_model, serving_version}. holder ∈ {llm, vision, training, null}; the version is the
    registry @serving pointer. Read-only; the API key never reaches the browser (BFF contract)."""
    state = await gpu_state()
    state["serving_version"] = await _resolve_serving_version()
    return state


@router.get("/serving/tasks")
async def serving_tasks():
    """Tasks discovered from the registry, one per model's `@serving` version (009 US1, FR-077).

    The Infer tab queries this to render one panel per `task` (dynamic) — adding a modality means
    registering a model with a `task` tag + dropping in a small renderer, not re-plumbing the UI. A
    serving version with no `task` tag is reported as task=None → the UI shows a read-only "no
    renderer" placeholder. Best-effort: returns an empty list if the registry is unreachable, so the
    tab still renders the always-on panels rather than erroring."""
    try:
        return {"tasks": await run_in_threadpool(registry.list_tasks)}
    except Exception:
        return {"tasks": []}
