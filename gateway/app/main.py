"""MLOps-Lite API gateway — single entry point.

Phase 2: health + metrics. Phase 3: on-demand LLM inference via the /infer router.
Phase 4: model registry via the /models router. Phase 5: dataset registry via /datasets.
Phase 6: training runs via the /runs router. Phase 7: drift monitoring + retrain loop via /monitor.
Phase 8: GPU/daemon metrics proxied into /metrics; OpenAPI exported for contracts.

002 hardening (US1): the lifecycle routers below require an API key (FR-016); `/healthz`,
`/metrics`, and `/` stay open for liveness and Prometheus.
"""
import os

from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from . import platform_health, platform_metrics, tracing
from .auth import auth_mode, require_api_key
from .routers import (
    batch,
    datasets,
    embed,
    infer,
    models,
    monitor,
    policies as policies_router,
    runs,
    stream,
    tabular,
    transcribe,
    validation,
    vision,
)

app = FastAPI(title="MLOps-Lite Gateway", version="1.2.0")

# Protected lifecycle routers — every route requires a valid API key when auth is enabled (T045).
_protected = [Depends(require_api_key)]
app.include_router(infer.router, tags=["inference"], dependencies=_protected)
app.include_router(models.router, tags=["registry"], dependencies=_protected)
app.include_router(datasets.router, tags=["datasets"], dependencies=_protected)
app.include_router(runs.router, tags=["training"], dependencies=_protected)
app.include_router(monitor.router, tags=["monitoring"], dependencies=_protected)
app.include_router(vision.router, tags=["vision"], dependencies=_protected)
app.include_router(stream.router, tags=["streaming"], dependencies=_protected)
app.include_router(embed.router, tags=["embeddings"], dependencies=_protected)  # 009 US2 (CPU, off-lease)
app.include_router(transcribe.router, tags=["asr"], dependencies=_protected)  # 009 US3 (whisper.cpp, GPU-lease)
app.include_router(tabular.router, tags=["tabular"], dependencies=_protected)  # 009 US4 (CPU, off-lease)
app.include_router(validation.router, tags=["validation"], dependencies=_protected)  # 014 US2
app.include_router(batch.router, tags=["batch"], dependencies=_protected)  # 014 US1 (offline batch)
app.include_router(policies_router.router, tags=["policies"], dependencies=_protected)  # 018 US3

REQUESTS = Counter("gateway_requests_total", "Total gateway requests", ["route"])


@app.get("/healthz")
def healthz():
    """Liveness probe (FR-015)."""
    REQUESTS.labels(route="/healthz").inc()
    return {"status": "ok", "service": "gateway"}


@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint (FR-015); also re-exports native-daemon GPU/state (T043)."""
    platform_metrics.refresh()
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/platform/health")
async def platform_health_endpoint():
    """Aggregated native-daemon reachability (002 US2, T051) — open like /healthz, for probes."""
    REQUESTS.labels(route="/platform/health").inc()
    return await platform_health.aggregate()


@app.get("/")
def root():
    return {
        "service": "mlops-lite-gateway",
        "version": app.version,
        "endpoints": [
            "/healthz", "/metrics", "/platform/health", "/infer", "/serving/health", "/serving/state",
            "/models", "/models/{name}", "/models/{name}/promote",
            "/models/{name}/evaluate", "/models/{name}/compare",
            "/datasets", "/datasets/{name}", "/datasets/{name}/{version}",
            "/datasets/{name}/{version}/validate",
            "/runs", "/runs/{id}", "/runs/{id}/events", "/training/health",
            "/studies", "/studies/{id}",
            "/batch", "/batch/{id}",
            "/monitor", "/monitor/check", "/monitor/labels",
            "/monitor/quality", "/monitor/quality/check",
            "/vision/classify", "/vision/health",
            "/embed", "/embed/health",
            "/transcribe", "/transcribe/health",
            "/predict", "/predict/health",
            "/infer/stream", "/platform/events", "/serving/tasks",
        ],
        "phase": "8 (complete) + 002 (auth/supervisor) + 003 (streaming UI) + 004 (BFF hardening) "
        "+ 005 (fail-closed auth) + 006 (inference tracing) + 007 (MLflow 3.x) + 008 (GPU lease) "
        "+ 009 (modalities) "
        "+ 010 (multimodal fine-tune) + 011 (eval gates) + 012 (HPO) + 013 (quality) "
        "+ 014 (batch/validation)",
        "auth_mode": auth_mode(),
        "tracing": tracing.enabled(),
        "trace_capture": tracing.capture_io(),
    }


# --- 018 US3 (FR-180): the policy scheduler — the loop runs without an external trigger -------------
# A lifespan asyncio task (research R5). Fail-open by construction: a tick with no policies is a
# no-op, per-policy errors are contained inside tick(), and the whole loop is disable-able.
_scheduler_stop = None


@app.on_event("startup")
async def _start_policy_scheduler():
    global _scheduler_stop
    if os.getenv("POLICY_SCHEDULER_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    import asyncio

    from . import scheduler as _scheduler

    _scheduler_stop = asyncio.Event()
    asyncio.ensure_future(_scheduler.PolicyScheduler().run(_scheduler_stop))


@app.on_event("shutdown")
async def _stop_policy_scheduler():
    if _scheduler_stop is not None:
        _scheduler_stop.set()
