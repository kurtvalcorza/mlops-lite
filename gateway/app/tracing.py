"""Inference tracing (006) — fire-and-forget MLflow traces, fail-open.

Manual instrumentation of the gateway's inference proxy paths. `mlflow.autolog()` is a no-op here
(the gateway proxies over httpx — there is no in-process LLM client to patch), so traces are emitted
manually. They are emitted **off the request path**: the route captures cheap data inline, then this
module backgrounds the *synchronous* MLflow span build+export (verified to block the caller
~45 ms-to-timeout) so neither the inference response nor the event loop ever waits on it.

The MLflow client is initialized **lazily on the worker thread** (not once at import): the gateway can
start before MLflow is ready, and tracing must self-heal once the server is reachable (and survive an
MLflow restart) without a gateway restart. The blocking init therefore runs inside the background
worker, never on the event loop.

API note (006/FR-048): uses the pinned `mlflow-skinny==2.18.0` low-level client API with explicit
timestamps — `MlflowClient.start_trace(..., start_time_ns=...)` + `end_trace(..., end_time_ns=...)` —
NOT the 3.x `mlflow.start_span_no_context` (absent here) nor the wall-clock `@mlflow.trace` /
`mlflow.start_span` context managers (they cannot backdate a span to the real request window).

Everything is best-effort: any tracing error is swallowed; inference is never affected.
"""
import logging
import os
import threading
import time

logger = logging.getLogger("gateway.tracing")

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in _TRUTHY


EXPERIMENT = os.getenv("MLFLOW_TRACING_EXPERIMENT", "mlops-lite-inference")

_ENABLED = _env_flag("MLFLOW_TRACING_ENABLED", True)
_CAPTURE_IO = _env_flag("MLFLOW_TRACE_CAPTURE_IO", True)

_client = None
_experiment_id = None
_init_lock = threading.Lock()
_last_init_monotonic = 0.0
_INIT_RETRY_SEC = 30.0  # when MLflow is unreachable, retry init at most this often (off-path)

# Trace exports run on bounded DAEMON threads — never FastAPI's request threadpool, never the registry
# client's HTTP path, and (being daemon) never blocking process/test shutdown even if one export is stuck
# on an unreachable MLflow. The semaphore caps concurrency: drop a trace rather than pile up threads when
# MLflow is slow. Keeps registry timeouts robust (no global MLFLOW_HTTP_* override) AND gives tracing a
# clean fail-open shutdown path (006 Codex review).
_MAX_CONCURRENT_EXPORTS = 2
_export_sem = threading.BoundedSemaphore(_MAX_CONCURRENT_EXPORTS)


def _silence_span_warning() -> None:
    """Mute the benign 2.18 `'MlflowSpanProcessor' object has no attribute '_metrics'` AttributeError
    logged on span-end (the trace still exports) — it surfaces via the tracing loggers AND the
    tracking-client logger. Must be REAPPLIED after the lazy `import mlflow`: MLflow's import resets
    these levels, so setting them only at module-import time wouldn't stick (006 Codex review)."""
    for _name in ("mlflow.tracing.fluent", "mlflow.tracing.export.mlflow", "mlflow.tracking.client"):
        logging.getLogger(_name).setLevel(logging.ERROR)


def _configure() -> None:
    """Import-time, network-free setup: silence the cosmetic 2.18 span-end warning."""
    if not _ENABLED:
        logger.info("inference tracing DISABLED (MLFLOW_TRACING_ENABLED is falsy)")
        return
    # NB: do NOT set MLFLOW_HTTP_REQUEST_TIMEOUT / MLFLOW_HTTP_REQUEST_MAX_RETRIES here — they are
    # PROCESS-WIDE and would also throttle the registry client (registry.py shares the MLflow stack for
    # /models, promotion, and _resolve_serving_version, which must keep MLflow's robust default
    # timeout/retries). The exporter is bounded instead by its own daemon threads (_export_sem-capped),
    # so a slow MLflow can't pin request threads, change registry behavior, or block shutdown (006 Codex).
    _silence_span_warning()  # reapplied after the lazy MLflow import too (see _ensure_client)
    logger.info("inference tracing ENABLED (experiment=%s, capture_io=%s) — client inits lazily",
                EXPERIMENT, _CAPTURE_IO)


def enabled() -> bool:
    """True when tracing is configured on (MLFLOW_TRACING_ENABLED). Export readiness is resolved
    lazily off-path; this stays the cheap request-path gate so a disabled flag has zero overhead."""
    return _ENABLED


def capture_io() -> bool:
    """True when prompt/output bodies may be captured (MLFLOW_TRACE_CAPTURE_IO, default on)."""
    return _CAPTURE_IO


def _ensure_client():
    """Lazily (re)create the MLflow client — BLOCKING, runs on the worker thread only. Throttled so a
    down server is retried at most every _INIT_RETRY_SEC. Returns the client, or None if not ready."""
    global _client, _experiment_id, _last_init_monotonic
    if _client is not None:
        return _client
    if not _ENABLED:
        return None
    with _init_lock:
        if _client is not None:
            return _client
        now = time.monotonic()
        if _last_init_monotonic and (now - _last_init_monotonic) < _INIT_RETRY_SEC:
            return None
        _last_init_monotonic = now
        try:
            import mlflow
            from mlflow import MlflowClient

            _silence_span_warning()  # MLflow's import resets these loggers — reapply (006 Codex review)
            mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000"))
            exp = mlflow.set_experiment(EXPERIMENT)
            _experiment_id = exp.experiment_id
            _client = MlflowClient()
            logger.info("inference tracing client ready (experiment=%s id=%s)",
                        EXPERIMENT, _experiment_id)
        except Exception as e:  # MLflow not ready yet — try again later, off-path.
            logger.debug("tracing client init deferred (%s)", e)
            _client = None
    return _client


def _emit_sync(name, inputs, outputs, attributes, start_ns, end_ns, status) -> None:
    """Build + export one trace synchronously (runs on a worker thread). Never raises."""
    client = _ensure_client()
    if client is None:
        return
    try:
        span = client.start_trace(
            name=name,
            inputs=inputs if _CAPTURE_IO else None,
            attributes=attributes or {},
            start_time_ns=start_ns,
            experiment_id=_experiment_id,
        )
        client.end_trace(
            request_id=span.request_id,
            outputs=outputs if _CAPTURE_IO else None,
            status=status,
            end_time_ns=end_ns,
        )
    except Exception as e:  # noqa: BLE001 — best-effort; tracing must never affect inference.
        logger.debug("trace emit failed (ignored): %s", e)


def emit(name, *, inputs=None, outputs=None, attributes=None, start_ns, end_ns, status="OK") -> None:
    """Fire-and-forget, fail-open: run the blocking span build+export on a bounded DAEMON thread.

    Returns immediately; the synchronous MLflow init+export never blocks the inference response or the
    event loop (FR-051), and being daemon it never blocks process/test shutdown (006 Codex review).
    """
    if not _ENABLED:
        return
    if not _export_sem.acquire(blocking=False):
        # Backpressure: max concurrent exports already in flight (MLflow slow) — drop this trace.
        return

    def _run():
        try:
            _emit_sync(name, inputs, outputs, attributes, start_ns, end_ns, status)
        finally:
            _export_sem.release()

    try:
        threading.Thread(target=_run, name="trace-export", daemon=True).start()
    except Exception as e:  # noqa: BLE001 — tracing must never affect inference
        _export_sem.release()
        logger.debug("trace schedule failed (ignored): %s", e)


_configure()
