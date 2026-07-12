"""T363 — platform health/metrics derive from the agent's single /health (offline).

The 6 per-daemon probes collapsed to one read of the host agent's `/health`; per-daemon reachability
is derived from its `engines: {id: state}` map (+ the agent being up for the jobs surface). Pins:
a servable engine → reachable, an unavailable one → not, an opt-in ASR absence doesn't fail
`all_healthy`, an unreachable agent marks everything down. `platform_metrics` derives the same.
Both use `from . import settings`, so they load under a stubbed `app` package (suite convention).
"""
import asyncio
import importlib.util
import os
import sys
import types

import pytest
from prometheus_client import REGISTRY as _PROM

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate_app_modules():
    """These tests stub `app.settings` + load `app.platform_*` into sys.modules. Snapshot & restore
    the `app*` entries so the stub can't leak into other suites (e.g. test_policy_scheduler reads
    the real settings) — a real test-ordering bug when this file sorts before them. Also unregister
    any Prometheus collectors a `platform_metrics` (re)load added, so a second load doesn't hit a
    duplicate-timeseries error in the global default registry."""
    saved = {k: sys.modules[k] for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    collectors = set(_PROM._collector_to_names)
    yield
    for k in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
        sys.modules.pop(k, None)
    sys.modules.update(saved)
    for c in list(_PROM._collector_to_names):
        if c not in collectors:
            _PROM.unregister(c)


def _load(mod_name):
    if "app" not in sys.modules:
        pkg = types.ModuleType("app")
        pkg.__path__ = [os.path.join(REPO, "gateway", "app")]
        sys.modules["app"] = pkg
    stub = types.ModuleType("app.settings")
    for k in ("AGENT_URL", "SERVING_URL", "TRAINER_URL", "BENTO_URL", "EMBED_URL", "TABULAR_URL",
              "ASR_URL"):
        setattr(stub, k, "http://agent.test" if k == "AGENT_URL" else f"http://{k.lower()}.test")
    stub.AGENT_API_KEY = ""
    stub.agent_headers = lambda: {}  # 023 US2: the real settings injects X-Agent-Key here
    sys.modules["app.settings"] = stub
    sys.modules["app"].settings = stub  # attribute too — `from . import settings` prefers it
    spec = importlib.util.spec_from_file_location(
        f"app.{mod_name}", os.path.join(REPO, "gateway", "app", f"{mod_name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"app.{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _AsyncClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if self._resp is None:
            raise RuntimeError("agent unreachable")
        return self._resp


def _aggregate_with(mod, resp, monkeypatch):
    # monkeypatch (auto-restored) — NOT `mod.httpx.X = ...`, which patches the shared global httpx
    # module and would leak the fake client into every later suite (e.g. the scheduler's poll).
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _AsyncClient(resp))
    return asyncio.run(mod.aggregate())


_HEALTHY = {"engines": {"llm": "ready", "vision": "cold", "embed": "cold", "tabular": "loading",
                        "asr": "cold"}, "busy": False}


def test_all_engines_servable_is_all_healthy(monkeypatch):
    out = _aggregate_with(_load("platform_health"), _Resp(200, _HEALTHY), monkeypatch)
    assert out["all_healthy"] is True
    assert out["daemons"]["serving"]["reachable"] and out["daemons"]["vision"]["reachable"]
    assert out["daemons"]["training"]["reachable"]          # jobs surface = agent up


def test_unavailable_required_engine_fails_all_healthy(monkeypatch):
    body = {"engines": {**_HEALTHY["engines"], "vision": "unavailable"}}
    out = _aggregate_with(_load("platform_health"), _Resp(200, body), monkeypatch)
    assert out["daemons"]["vision"]["reachable"] is False
    assert out["all_healthy"] is False


def test_optin_asr_absence_does_not_fail_all_healthy(monkeypatch):
    body = {"engines": {**_HEALTHY["engines"], "asr": "unavailable"}}
    out = _aggregate_with(_load("platform_health"), _Resp(200, body), monkeypatch)
    assert out["daemons"]["asr"]["reachable"] is False and out["daemons"]["asr"]["optional"]
    assert out["all_healthy"] is True                        # ASR is opt-in


def test_agent_unreachable_marks_everything_down(monkeypatch):
    out = _aggregate_with(_load("platform_health"), None, monkeypatch)
    assert out["all_healthy"] is False
    assert all(not d["reachable"] for d in out["daemons"].values())


def test_metrics_refresh_derives_gauges_from_agent_health(monkeypatch):
    mod = _load("platform_metrics")

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp(200, {"engines": {"llm": "ready"}, "busy": True, "gpu_free_mib": 4096})

    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: _Client())
    mod.refresh()
    assert mod.SERVING_RESIDENT._value.get() == 1            # engines.llm == ready
    assert mod.TRAINER_BUSY._value.get() == 1 and mod.GPU_FREE._value.get() == 4096
    assert mod.SERVING_UP._value.get() == 1 and mod.TRAINER_UP._value.get() == 1


def test_metrics_serving_resident_true_while_loading(monkeypatch):
    # @claude PR#37: the child-alive `resident` flag was true during cold-start — SERVING_RESIDENT
    # must read 1 for `loading`, not just `ready` (else the gauge dips to 0 mid-cold-start).
    mod = _load("platform_metrics")

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp(200, {"engines": {"llm": "loading"}, "busy": False})

    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: _Client())
    mod.refresh()
    assert mod.SERVING_RESIDENT._value.get() == 1            # loading child is resident


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
