"""MLOps-Lite API gateway — single entry point.

Phase 2: health + metrics. Phase 3: on-demand LLM inference via the /infer router.
Phase 4: model registry via the /models router. Phase 5: dataset registry via /datasets.
Phase 6: training runs via the /runs router. Phase 7: drift monitoring + retrain loop via /monitor.
Phase 8: GPU/daemon metrics proxied into /metrics; OpenAPI exported for contracts.

002 hardening (US1): the lifecycle routers below require an API key (FR-016); `/healthz`,
`/metrics`, and `/` stay open for liveness and Prometheus.
"""
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from . import platform_health, platform_metrics
from .auth import require_api_key
from .routers import datasets, infer, models, monitor, runs, stream, vision

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
            "/healthz", "/metrics", "/platform/health", "/infer", "/serving/health",
            "/models", "/models/{name}", "/models/{name}/promote",
            "/datasets", "/datasets/{name}", "/datasets/{name}/{version}",
            "/runs", "/runs/{id}", "/runs/{id}/events", "/training/health",
            "/monitor", "/monitor/check",
            "/vision/classify", "/vision/health",
            "/infer/stream", "/platform/events",
        ],
        "phase": "8 (complete) + 002 US1 (auth) + US2 (supervisor) + 003 US1 (streaming)",
    }
