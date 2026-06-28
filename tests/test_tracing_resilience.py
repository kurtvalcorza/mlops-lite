"""006 US3 tracing resilience + toggle test (T112, FR-051/FR-052 / SC-034).

Deterministic, offline checks of the tracing module's contract by loading a fresh copy under different
env (mirrors test_auth_modes): the enable/capture toggles resolve correctly, a disabled flag makes
`emit()` a no-op that schedules nothing, and `emit()` is fail-open (never raises even with no MLflow).

The live fail-open property (MLflow stopped → inference still 200, no added latency, traces self-heal
on restart) is validated manually on the target machine (SC-033) — stopping MLflow mid-suite would
disrupt the running stack, so it is not automated here.

Skips cleanly if FastAPI isn't importable (tracing.py imports fastapi.concurrency).
"""
import asyncio
import importlib.util
import os

import pytest

pytest.importorskip("fastapi")  # tracing.py imports fastapi.concurrency.run_in_threadpool

TRACING_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway", "app", "tracing.py"
)
_TRACE_ENV = ("MLFLOW_TRACING_ENABLED", "MLFLOW_TRACE_CAPTURE_IO", "MLFLOW_TRACING_EXPERIMENT")


def _load_tracing(**env):
    """Load a fresh, isolated copy of tracing.py with the given env (others cleared)."""
    saved = {k: os.environ.get(k) for k in _TRACE_ENV}
    try:
        for k in _TRACE_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is not None:
                os.environ[k] = v
        spec = importlib.util.spec_from_file_location("tracing_under_test", TRACING_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_default_enabled_and_capture():
    m = _load_tracing()
    assert m.enabled() is True
    assert m.capture_io() is True


def test_disabled_flag_bypasses_and_emits_nothing():
    m = _load_tracing(MLFLOW_TRACING_ENABLED="0")
    assert m.enabled() is False
    # emit() must be a no-op: returns immediately, schedules no background task, never raises.
    m.emit("infer", inputs={"prompt": "x"}, attributes={}, start_ns=1, end_ns=2)
    assert len(m._tasks) == 0


def test_capture_io_off_keeps_tracing_on():
    m = _load_tracing(MLFLOW_TRACE_CAPTURE_IO="0")
    assert m.enabled() is True
    assert m.capture_io() is False


def test_emit_is_fail_open_with_no_mlflow():
    """With tracing enabled but no reachable MLflow, emit() must never raise on the request path."""
    m = _load_tracing(MLFLOW_TRACING_ENABLED="1")

    async def _drive():
        # Point at a dead tracking server; emit schedules an off-path worker that must swallow the
        # failure. The call itself must return without raising.
        os.environ["MLFLOW_TRACKING_URI"] = "http://127.0.0.1:9"
        m.emit("infer", inputs={"prompt": "x"}, attributes={"model": "m"}, start_ns=1, end_ns=2)
        # let the scheduled worker run and fail quietly
        await asyncio.sleep(0.2)

    asyncio.run(_drive())  # must not raise


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_falsy_values_disable(val):
    assert _load_tracing(MLFLOW_TRACING_ENABLED=val).enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_truthy_values_enable(val):
    assert _load_tracing(MLFLOW_TRACING_ENABLED=val).enabled() is True
