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
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from . import platform_health, platform_metrics, tracing
from .auth import auth_mode, require_api_key
from .routers import (
    batch,
    datasets,
    embed,
    infer,
    models,
    monitor,
    runs,
    stream,
    tabular,
    transcribe,
    validation,
    vision,
)
from .routers import (
    policies as policies_router,
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
    """Liveness + readiness probe (FR-015; 023 US4 FR-299: a migration failure fails readiness —
    the platform must not look healthy while its schema is wrong-shaped or unapplied)."""
    REQUESTS.labels(route="/healthz").inc()
    if _MIGRATION_STATUS["state"] == "error":
        return JSONResponse(status_code=503, content={
            "status": "error", "service": "gateway",
            "migrations": _MIGRATION_STATUS["error"]})
    return {"status": "ok", "service": "gateway"}


@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint (FR-015); also re-exports native-daemon GPU/state (T043)."""
    platform_metrics.refresh()
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/platform/health")
async def platform_health_endpoint():
    """Aggregated native-daemon reachability (002 US2, T051) — open like /healthz, for probes.
    023 T516: carries the sanitized migration verdict too (state/version/error — no DSN/SQL)."""
    REQUESTS.labels(route="/platform/health").inc()
    out = await platform_health.aggregate()
    out["migrations"] = migration_status()
    return out


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


# --- 023 US4 (T514, FR-299): gateway startup OWNS schema migrations ---------------------------------
# Applied before anything serves; a migration failure fails readiness (the status below reads
# "error" and /healthz flips 503) rather than letting requests run against a wrong-shaped schema.
# Fail-CLOSED but not crash-looped: the gateway stays up to report WHAT failed via
# /platform/health, and the metrics gauges expose version/pending (T516) — no DSN/SQL in any of it.
_MIGRATION_STATUS = {"state": "pending", "db_version": None, "applied": [], "error": None}

_MIG_DB_VERSION = Gauge("mlops_migrations_db_version", "Applied gateway-DB schema version")
_MIG_PENDING = Gauge("mlops_migrations_pending", "Migrations shipped by this binary not yet applied")
_MIG_OUTCOMES = Counter("mlops_migrations_outcomes_total", "Migration apply outcomes", ["outcome"])
_MIG_DURATION = Gauge("mlops_migrations_last_apply_ms", "Duration of the last migration apply run")


def migration_status() -> dict:
    """The sanitized migration verdict for /platform/health (T516) — never a DSN or SQL text."""
    return dict(_MIGRATION_STATUS)


@app.on_event("startup")
def _apply_migrations():
    if os.getenv("GATEWAY_MIGRATIONS_ENABLED", "1").lower() in ("0", "false", "no"):
        _MIGRATION_STATUS["state"] = "disabled"
        return
    import time as _time

    from platformlib import migrations

    last_err = None
    for attempt in range(int(os.getenv("GATEWAY_MIGRATIONS_RETRIES", "5"))):
        if attempt:
            _time.sleep(min(2.0 * attempt, 8.0))  # Postgres may still be coming up alongside us
        try:
            report = migrations.apply(applied_by="gateway")
            _MIGRATION_STATUS.update(state="ok", db_version=report["db_version"],
                                     applied=report["applied"], error=None)
            _MIG_DB_VERSION.set(report["db_version"])
            _MIG_PENDING.set(0)
            _MIG_DURATION.set(report["duration_ms"])
            _MIG_OUTCOMES.labels(outcome="ok").inc()
            return
        except migrations.MigrationError as e:
            # A REAL migration failure (checksum drift, newer schema, failed SQL) is terminal —
            # retrying cannot fix bytes; surface it at once.
            last_err = f"{e}"
            break
        except Exception as e:  # noqa: BLE001 — connection-level: retry while Postgres warms up
            last_err = f"database unreachable ({e.__class__.__name__})"
    _MIGRATION_STATUS.update(state="error", error=last_err)
    _MIG_OUTCOMES.labels(outcome="error").inc()


# --- 023 US5 (T524, FR-309..311): activation reconciliation from gateway lifespan -------------------
# One pass right after migrations (resume whatever a crash/restart stranded), then periodic while
# non-terminal operations exist. Bounded and contained: one stuck operation cannot stall the loop,
# and the loop never blocks unrelated startup (it runs as a background task, in the threadpool).
_reconcile_stop = None
_reconcile_task = None  # strong reference — a bare ensure_future task can be GC'd mid-flight


@app.get("/serving/llm/activation", dependencies=_protected)
def serving_llm_activation():
    """The desired/resident/activation read model (023 US5 — contracts/promotion-activation.md
    §Read): `desired` is the pointer, `resident` is agent-reported, `consistent` only when they
    agree in a terminal-success state. Additive; no secret/path/exception internals.

    Protected with the gateway API key (review, Codex): as a bare `@app.get` it otherwise bypassed
    the `_protected` dependency its `/serving/state`+`/serving/tasks` siblings (infer.router) carry,
    leaking desired/resident identities, operation ids, and activation errors to unauthenticated
    callers when gateway auth is enabled."""
    REQUESTS.labels(route="/serving/llm/activation").inc()
    from . import activation
    return activation.service().read_model()


@app.on_event("startup")
async def _start_activation_reconciler():
    global _reconcile_stop, _reconcile_task
    if os.getenv("ACTIVATION_RECONCILER_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    import asyncio

    from fastapi.concurrency import run_in_threadpool

    from . import activation

    interval = float(os.getenv("ACTIVATION_RECONCILE_INTERVAL_S", "60"))
    _reconcile_stop = asyncio.Event()

    async def loop():
        while not _reconcile_stop.is_set():
            try:
                pending = await run_in_threadpool(activation.service().pending_count)
                if pending:
                    await run_in_threadpool(activation.service().reconcile_all)
            except Exception:  # noqa: BLE001 — store/agent blip: try again next tick
                pass
            try:
                await asyncio.wait_for(_reconcile_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    _reconcile_task = asyncio.ensure_future(loop())


@app.on_event("shutdown")
async def _stop_activation_reconciler():
    if _reconcile_stop is not None:
        _reconcile_stop.set()


# --- 018 US3 (FR-180): the policy scheduler — the loop runs without an external trigger -------------
# A lifespan asyncio task (research R5). Fail-open by construction: a tick with no policies is a
# no-op, per-policy errors are contained inside tick(), and the whole loop is disable-able.
_scheduler_stop = None
_scheduler_task = None  # strong reference — a bare ensure_future task can be GC'd mid-flight
# (Codex round 2, 018: the same weak-reference foot-gun background.spawn() was added to close).


@app.on_event("startup")
async def _start_policy_scheduler():
    global _scheduler_stop, _scheduler_task
    if os.getenv("POLICY_SCHEDULER_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    import asyncio

    from . import scheduler as _scheduler

    _scheduler_stop = asyncio.Event()
    _scheduler_task = asyncio.ensure_future(_scheduler.PolicyScheduler().run(_scheduler_stop))


@app.on_event("shutdown")
async def _stop_policy_scheduler():
    if _scheduler_stop is not None:
        _scheduler_stop.set()
