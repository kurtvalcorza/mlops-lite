"""023 US1 (T493, FR-277..280) — live-evaluation predictors dial the CONSOLIDATED agent topology.

Pre-023, `_predict_llm`/`_predict_vision` read `SERVING_URL`/`BENTO_URL` with the retired pre-018
per-daemon defaults (`:8090` / `:8092`) — every unconfigured live evaluation dialed ports nothing
has listened on since the agent fold-in. Pins here:

  - the module still loads standalone (via `platformlib.gateway_bridge`, zero third-party imports
    needed for the pure metric logic — FR-279);
  - with only `AGENT_URL`, both predictors resolve under `<agent>/engines/<id>` (FR-277) and hit
    exactly `/engines/llm/infer` and `/engines/vision/classify` (FR-278) — proven against a FAKE
    httpx client so no network is touched;
  - explicit `SERVING_URL`/`BENTO_URL` overrides still win (a deliberately configured deployment
    keeps working);
  - the retired daemon ports appear in NO resolved default (FR-280's runtime half — the source-scan
    half lives in scripts/check_specs.py).
"""
import base64
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _pkgload import fresh_package, load_in_package  # noqa: E402

RETIRED_PORTS = (":8090", ":8091", ":8092", ":8093", ":8094", ":8095")  # retired-port-ok (negative fixture)


@pytest.fixture()
def evaluation(monkeypatch):
    """A freshly loaded evaluation module with a CLEAN URL environment (no stale overrides)."""
    for var in ("SERVING_URL", "BENTO_URL", "AGENT_URL"):
        monkeypatch.delenv(var, raising=False)
    return load_in_package(fresh_package(), "evaluation")


def test_loads_standalone_through_the_bridge():
    """FR-279: the standalone load path every training/scoring consumer uses still works."""
    from platformlib.gateway_bridge import evaluation as load_ev
    ev = load_ev()
    assert callable(ev.evaluate)


def test_llm_default_derives_from_agent_topology(evaluation, monkeypatch):
    monkeypatch.setenv("AGENT_URL", "http://10.0.0.7:8100")
    assert evaluation._engine_base("llm", "SERVING_URL") == "http://10.0.0.7:8100/engines/llm"


def test_vision_default_derives_from_agent_topology(evaluation, monkeypatch):
    monkeypatch.setenv("AGENT_URL", "http://10.0.0.7:8100")
    assert evaluation._engine_base("vision", "BENTO_URL") == "http://10.0.0.7:8100/engines/vision"


def test_unconfigured_defaults_carry_no_retired_daemon_port(evaluation):
    """FR-277/280: with NOTHING configured, the defaults are the agent's — never :8090/:8092."""
    for engine, env in (("llm", "SERVING_URL"), ("vision", "BENTO_URL")):
        url = evaluation._engine_base(engine, env)
        assert url.endswith(f":8100/engines/{engine}")
        assert not any(p in url for p in RETIRED_PORTS)


def test_explicit_overrides_still_win(evaluation, monkeypatch):
    monkeypatch.setenv("SERVING_URL", "http://custom-llm:9000")
    monkeypatch.setenv("BENTO_URL", "http://custom-vision:9001")
    assert evaluation._engine_base("llm", "SERVING_URL") == "http://custom-llm:9000"
    assert evaluation._engine_base("vision", "BENTO_URL") == "http://custom-vision:9001"


# --- fake-HTTP path assertions (FR-278): the exact verbs, no network ------------------------------

class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Records every POST url; returns a canned payload."""
    def __init__(self, calls, payload):
        self._calls, self._payload = calls, payload

    def __call__(self, timeout=None):  # httpx.Client(timeout=...)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kw):
        self._calls.append(url)
        return _FakeResponse(self._payload)


def _fake_httpx(monkeypatch, calls, payload):
    fake = types.ModuleType("httpx")
    fake.Client = _FakeClient(calls, payload)
    monkeypatch.setitem(sys.modules, "httpx", fake)


def test_predict_llm_posts_engines_llm_infer(evaluation, monkeypatch):
    calls = []
    _fake_httpx(monkeypatch, calls, {"text": "ok"})
    out = evaluation._predict_llm([{"prompt": "2+2?"}], "text-generation", "1")
    assert out == ["ok"]
    assert calls == ["http://host.docker.internal:8100/engines/llm/infer"]


def test_predict_vision_posts_engines_vision_classify(evaluation, monkeypatch):
    calls = []
    _fake_httpx(monkeypatch, calls, {"predictions": [{"label": "cat"}]})
    row = {"image_b64": base64.b64encode(b"png-bytes").decode()}
    out = evaluation._predict_vision([row], "image-classification", "1")
    assert out == ["cat"]
    assert calls == ["http://host.docker.internal:8100/engines/vision/classify"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
