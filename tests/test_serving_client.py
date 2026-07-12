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
    settings_stub.AGENT_URL = "http://serving.test"
    settings_stub.AGENT_CONTROL_SECRET = ""
    # 023 US2: the internal agent credential the real settings injects (T498 pins the injection)
    settings_stub.AGENT_API_KEY = "test-agent-key"
    settings_stub.agent_headers = lambda: {"X-Agent-Key": settings_stub.AGENT_API_KEY}
    sys.modules["app.settings"] = settings_stub
    # Bind the ATTRIBUTE too: when an earlier suite imported the real `app` package,
    # `from . import settings` resolves through the package attribute (already bound to the real
    # module) and silently bypasses the sys.modules stub above.
    sys.modules["app"].settings = settings_stub
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
    def __init__(self, resp, seen=None, ctor_kwargs=None):
        self._resp = resp
        self._seen = seen
        self.ctor_kwargs = ctor_kwargs or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if self._seen is not None:
            self._seen["url"] = url
        return self._resp


def _run_inference_with(mod, resp, *, preempt=False, seen=None):
    def _ctor(**kw):
        if seen is not None:
            seen["ctor"] = kw
        return _Client(resp, seen, kw)

    mod.httpx.AsyncClient = _ctor
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


# --- 023 US2 (T498, FR-286): every agent call carries the internal credential ----------------------

def test_run_inference_injects_agent_key():
    mod = _load_serving()
    seen = {}
    _run_inference_with(mod, _Resp(200, {"text": "x"}), seen=seen)
    assert seen["ctor"]["headers"] == {"X-Agent-Key": "test-agent-key"}


def test_run_inference_never_follows_redirects():
    """FR-286: a redirect to a different origin must not carry the key onward — the client relies
    on httpx's follow-redirects-off default, so no call site may quietly enable it."""
    mod = _load_serving()
    seen = {}
    _run_inference_with(mod, _Resp(200, {"text": "x"}), seen=seen)
    assert "follow_redirects" not in seen["ctor"]


def test_no_gateway_client_enables_redirect_following():
    """Repo-level pin for FR-286: agent-directed httpx clients keep redirects disabled (the
    httpx default). A future `follow_redirects=True` toward the agent is a review flag, not a
    silent behavior change."""
    import re
    app_dir = os.path.join(REPO, "gateway", "app")
    offenders = []
    for root, _dirs, files in os.walk(app_dir):
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(root, f), encoding="utf-8").read()
                if re.search(r"follow_redirects\s*=\s*True", src):
                    offenders.append(f)
    assert not offenders, f"gateway clients must not follow redirects toward the agent: {offenders}"


def test_reload_request_sends_agent_key_and_control_header():
    """request_llm_reload keeps the deprecated X-Agent-Control (one-release compat) AND rides the
    keyed client — the agent's global gate sees X-Agent-Key on the reload too."""
    mod = _load_serving()
    seen = {}

    class _SyncClient:
        def __init__(self, **kw):
            seen["ctor"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            seen["url"], seen["post_headers"] = url, headers or {}
            return _Resp(200, {"status": "noop"})

    mod.httpx.Client = _SyncClient
    out = mod.request_llm_reload()
    assert out["status"] == "noop"
    assert seen["ctor"]["headers"] == {"X-Agent-Key": "test-agent-key"}
    assert seen["url"].endswith("/control/reload")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
