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
    ModelTooLargeError,
    ServingBusyError,
    ServingError,
    gpu_state,
    health,
    llm_identity,
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
    preempt: bool = False  # 017: opt-in swap — evict a resident *serving* model first (default 008 refuse)


async def _served_identity() -> dict:
    """The identity every surface attributes this inference to (022 US2, FR-260/261/262): the
    AGENT-REPORTED {serving_model, serving_version} — the agent is the only component that knows
    what is actually resident, so the response's registry_model/registry_version, the logged
    prediction, and `/serving/state` all name the same model+version (the pre-022 divergence —
    `model: ops-bot-v2` logged as `registry_model: qwen…` — cannot recur). When the agent doesn't
    report a registry version (env-default binding on a legacy agent), the version falls back to
    the reported MODEL's own `@serving` alias — still keyed to the served name, never to the
    fixed SERVING_MODEL config."""
    ident = await llm_identity()
    if ident["serving_model"] != "unknown" and ident["serving_version"] is None:
        try:
            served = await run_in_threadpool(registry.get_serving, ident["serving_model"])
            if served:
                ident["serving_version"] = served["version"]
        except Exception:
            pass  # best-effort — the response just omits the version
    return ident


@router.post("/infer")
async def infer(req: InferRequest):
    """Submit a text prompt; returns the completion and metadata, including cold-start load time.

    Emits one fire-and-forget MLflow trace per request (006/FR-049) — including error outcomes — from
    a `finally`, off the request path. The span is at the router, naturally outside the GPU lock.
    """
    start_ns = time.time_ns()
    result = None
    served_model, registry_version = "unknown", None
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
            # T363: forward `preempt` to the AGENT, which orchestrates the swap (evict a resident
            # serving holder; a `kind="job"` holder refuses structurally → the agent 409s → surfaces
            # as ServingBusyError below). The gateway no longer brokers the eviction.
            result = await run_inference(req.prompt, req.max_tokens, req.temperature,
                                         preempt=req.preempt)
        except ModelTooLargeError as e:
            INFER_REQUESTS.labels(status="rejected").inc()
            trace_status, outcome = "ERROR", "rejected"
            raise HTTPException(status_code=400, detail=str(e))
        except ServingBusyError as e:
            # 018 T358: another GPU tenant holds the slot (refuse-if-held). Contention, not an
            # outage — classify as `rejected` (like the pre-018 507-for-busy path), so alerting on
            # `error` doesn't misfire, and return 409 with the holder detail the agent surfaced.
            # Must precede the ServingError branch — ServingBusyError is a subclass.
            INFER_REQUESTS.labels(status="rejected").inc()
            trace_status, outcome = "ERROR", "rejected"
            raise HTTPException(status_code=409, detail=str(e))
        except ServingError as e:
            INFER_REQUESTS.labels(status="error").inc()
            trace_status, outcome = "ERROR", "error"
            raise HTTPException(status_code=502, detail=str(e))

        if result.get("load_ms"):
            LOAD_LATENCY.observe(result["load_ms"] / 1000.0)
        INFER_LATENCY.observe(result.get("infer_ms", 0) / 1000.0)
        INFER_REQUESTS.labels(status="ok").inc()
        # 022 US2 (FR-260/261/262): attribute everything — response identity, prediction log,
        # trace — to the AGENT-REPORTED served identity, not the fixed SERVING_MODEL config.
        ident = await _served_identity()
        served_model, registry_version = ident["serving_model"], ident["serving_version"]
        trace_status, outcome = "OK", "completed"  # success is known — flip the pessimistic default
        # 013/FR-119: log the served prediction off the request path (fire-and-forget, fail-open) so it
        # can be scored later against a delayed label. Returns a synchronous id regardless of store state.
        prediction_id = quality.log_prediction(
            served_model, registry_version, "text-generation", req.prompt, result.get("text"))
        # 016 (FR-146): also route the prompt to the bounded recoverable-input capture (uniform replay
        # corpus across modalities), so it can be shadow-replayed. The served decoding settings ride along
        # so a replay reproduces them, not the scorer defaults. Fire-and-forget + fail-open.
        quality.capture_input(prediction_id, "text-generation", req.prompt,
                              options={"max_tokens": req.max_tokens, "temperature": req.temperature})
        return {
            "status": "completed",
            "registry_model": served_model,
            "registry_version": registry_version,
            "prediction_id": prediction_id,
            **result,
        }
    finally:
        attrs = {
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "model": served_model,
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
    serving_model, serving_version, base, adapter}. holder ∈ {llm, vision, training, null}.

    022 (FR-260, T470): the model+version are the AGENT-REPORTED served identity (what is actually
    resident, or what the next load would serve when cold) — `unknown` when the agent is
    unreachable, never a stale config guess. `base`/`adapter` expose a served fine-tune's resolved
    provenance (FR-274). Read-only; the API key never reaches the browser (BFF contract)."""
    state = await gpu_state()
    if state["serving_model"] != "unknown" and state["serving_version"] is None:
        # Legacy-agent tolerance: no registry_version reported → the served NAME's own @serving
        # alias (still keyed to the reported name — the same rule as _served_identity).
        try:
            served = await run_in_threadpool(registry.get_serving, state["serving_model"])
            if served:
                state["serving_version"] = served["version"]
        except Exception:
            pass
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
