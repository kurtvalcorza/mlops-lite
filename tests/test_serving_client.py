"""018 T358 — gateway serving-client status mapping (offline, no live daemon).

Regression for the fold-in finding both review bots raised: the host agent returns **409** when
another GPU tenant holds the slot (refuse-if-held), but `run_inference` previously mapped every
non-200/507 to a generic `ServingError` → the `/infer` router surfaced it as an opaque **502**
`error` (flipping the Prometheus/MLflow status from `rejected` to `error`). The retired supervisor
expressed the same refusal as a 507. This pins 409 → `ServingBusyError` (a distinct, non-outage
"busy") so `/infer` keeps reporting contention as `rejected`, not a backend fault.

`gateway/app/serving.py` uses `from . import settings`, so it is loaded here under a stubbed `app`
package (the suite's standalone-import convention for gateway modules with a relative import).
"""
import asyncio
import importlib.util
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_serving():
    """Load gateway/app/serving.py as `app.serving` with a minimal fake `app` package so its
    `from . import settings` resolves without importing the whole gateway."""
    if "app" not in sys.modules:
        pkg = types.ModuleType("app")
        pkg.__path__ = [os.path.join(REPO, "gateway", "app")]
        sys.modules["app"] = pkg
    settings_stub = types.ModuleType("app.settings")
    settings_stub.SERVING_URL = "http://serving.test"
    settings_stub.SERVING_MODEL = "test-model"
    sys.modules["app.settings"] = settings_stub
    spec = importlib.util.spec_from_file_location(
        "app.serving", os.path.join(REPO, "gateway", "app", "serving.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app.serving"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _Client:
    def __init__(self, resp, seen=None):
        self._resp = resp
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if self._seen is not None:
            self._seen["url"] = url
        return self._resp


def _run_inference_with(mod, resp, *, preempt=False, seen=None):
    mod.httpx.AsyncClient = lambda **kw: _Client(resp, seen)
    return asyncio.run(mod.run_inference("hello", preempt=preempt))


def test_preempt_appends_query_to_agent_url():
    # T363: the gateway thinned its swap to forwarding the flag — run_inference must append
    # `?preempt=true` so the AGENT orchestrates the eviction; the default path stays a bare /infer.
    mod = _load_serving()
    seen = {}
    _run_inference_with(mod, _Resp(200, {"text": "x"}), preempt=True, seen=seen)
    assert seen["url"].endswith("/infer?preempt=true")
    seen.clear()
    _run_inference_with(mod, _Resp(200, {"text": "x"}), seen=seen)
    assert seen["url"].endswith("/infer") and "preempt" not in seen["url"]


def test_409_maps_to_serving_busy_error():
    mod = _load_serving()
    with pytest.raises(mod.ServingBusyError) as ei:
        _run_inference_with(mod, _Resp(409, {"error": "GPU busy: vision holds the slot",
                                             "holder": "vision", "kind": "serving"}))
    assert "vision" in str(ei.value)
    # a distinct, non-outage class — NOT lumped into the generic 502 ServingError bucket
    assert isinstance(ei.value, mod.ServingError)
    assert not isinstance(ei.value, mod.ModelTooLargeError)


def test_507_still_maps_to_model_too_large():
    mod = _load_serving()
    with pytest.raises(mod.ModelTooLargeError):
        _run_inference_with(mod, _Resp(507, {"error": "exceeds VRAM budget"}))


def test_other_non_200_is_generic_serving_error():
    mod = _load_serving()
    with pytest.raises(mod.ServingError) as ei:
        _run_inference_with(mod, _Resp(500, {"error": "boom"}))
    assert not isinstance(ei.value, mod.ServingBusyError)


def test_200_returns_body():
    mod = _load_serving()
    out = _run_inference_with(mod, _Resp(200, {"text": "hi", "model": "test-model"}))
    assert out["text"] == "hi"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
