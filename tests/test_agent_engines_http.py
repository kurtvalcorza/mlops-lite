"""018 T358 — the agent's /engines HTTP surface (offline, GPU-free; 020 T413 dual-runtime).

A real server over a fake LLM adapter drives the routing added by the LLM fold-in and its review
fixes — parameterized over BOTH transports (stdlib + uvicorn-ASGI) while the `AGENT_RUNTIME`
switch exists, so contract drift between them is a test failure (020 US3): per-engine health with
a 503 when the engine can't serve (adapter-unavailable OR runtime-wedged, Codex rounds 7/8),
strict path-shape rejection of malformed inference paths (round 8), the happy-path forward, the
symmetric multipart/JSON 415 guards, SSE stream framing byte-parity, the control-secret gate, and
the pre-stream JSON error path.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from _agentserver import RUNTIMES, start_agent  # noqa: E402
from _agentstore import FakeJobStore  # noqa: E402
from test_agent_lifecycle import FakeChild  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent import lifecycle  # noqa: E402
from hostagent.journal import Journal  # noqa: E402

SSE_FRAMES = (b'data: {"event": "start", "model": "m"}\n\n',
              b'data: {"event": "token", "text": "hi"}\n\n',
              b'data: {"event": "done"}\n\n')


@pytest.fixture(params=RUNTIMES)
def runtime(request):
    return request.param


class FakeLLM:
    engine_id = "llm"
    gpu = True
    optional = False
    verbs = ("infer",)
    stream_verbs = ("infer",)

    def __init__(self):
        self.avail = (True, None)
        self.stream_error = None
        self._children = []

    def available(self):
        return self.avail

    def estimate_vram(self):
        return 1.0

    def spawn(self):
        c = FakeChild(pid=5000 + len(self._children))
        self._children.append(c)
        return c

    def ready(self):
        return True

    def forward(self, verb, body, load_ms):
        return {"text": "ok", "load_ms": load_ms, "model": "m", "usage": {}}

    def stream(self, verb, body, load_ms):
        if self.stream_error is not None:
            raise self.stream_error
        yield from SSE_FRAMES

    def health(self, resident):
        ok, reason = self.avail
        p = {"ok": ok, "resident": resident, "model": "m"}
        if not ok:
            p["unavailable"] = reason
        return p


def _serve(adapter, runtime):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(store=FakeJobStore())
    rt = lifecycle.EngineRuntime(adapter, admission)
    manager = lifecycle.EngineManager(admission, runtimes={"llm": rt})
    server = start_agent(admission, journal, manager,
                         jobs_mod.JobManager(admission, journal), runtime)
    return server, server.base_url, rt


def _req(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def test_engine_health_200_when_serviceable(runtime):
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, body = _req(base, "/engines/llm/health")
        assert code == 200 and body["ok"] is True and body["model"] == "m"
        code, _ = _req(base, "/engines/nope/health")
        assert code == 404
    finally:
        server.shutdown()


def test_engine_health_503_when_adapter_unavailable(runtime):
    a = FakeLLM()
    a.avail = (False, "model GGUF not found")
    server, base, _ = _serve(a, runtime)
    try:
        code, body = _req(base, "/engines/llm/health")
        assert code == 503 and body["ok"] is False and "model" in body["unavailable"]
    finally:
        server.shutdown()


def test_engine_health_503_when_runtime_wedged(runtime):
    # Codex round 8: a wedged child (survived SIGKILL, GPU pinned) reports ok:true from the
    # adapter's file checks alone — the route must fold in the runtime state and answer 503.
    server, base, rt = _serve(FakeLLM(), runtime)
    try:
        rt.wedged_reason = "child pid=1 survived SIGKILL"
        code, body = _req(base, "/engines/llm/health")
        assert code == 503 and body["ok"] is False and "wedged" in body
    finally:
        server.shutdown()


def test_forward_happy_path(runtime):
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, body = _req(base, "/engines/llm/infer", "POST", {"prompt": "hi"})
        assert code == 200 and body["text"] == "ok"
    finally:
        server.shutdown()


def test_strict_path_rejects_extra_segments(runtime):
    # Codex round 8: /engines/llm/infer/typo and /engines/llm/infer/stream/extra must 404, not
    # silently forward as the bare `infer` verb.
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        for bad in ("/engines/llm/infer/typo", "/engines/llm/infer/stream/extra"):
            code, _ = _req(base, bad, "POST", {"prompt": "hi"})
            assert code == 404, f"{bad} should 404"
    finally:
        server.shutdown()


# -- SSE streaming (020 T413: identical framing on both transports) ------------------------------

def _post_stream(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read()


def test_stream_framing_byte_identical(runtime):
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, ctype, body = _post_stream(base, "/engines/llm/infer/stream", {"prompt": "hi"})
        assert code == 200
        assert ctype.startswith("text/event-stream")
        assert body == b"".join(SSE_FRAMES)          # the frames, verbatim, in order
    finally:
        server.shutdown()


def test_stream_unknown_stream_verb_404(runtime):
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, _, _ = _post_stream(base, "/engines/llm/nope/stream", {"prompt": "hi"})
        assert code == 404
    finally:
        server.shutdown()


def test_stream_prestream_failure_maps_to_json_error(runtime):
    # A failure BEFORE the first frame (backend connect refused, bad state) must answer a mapped
    # JSON error, not a half-open SSE response — on both transports.
    a = FakeLLM()
    a.stream_error = RuntimeError("backend connect refused")
    server, base, _ = _serve(a, runtime)
    try:
        code, ctype, body = _post_stream(base, "/engines/llm/infer/stream", {"prompt": "hi"})
        assert code == 502
        assert ctype.startswith("application/json")
        assert "backend connect refused" in json.loads(body)["error"]
    finally:
        server.shutdown()


def test_stream_releases_lock_for_next_request(runtime):
    # After a full stream, the runtime lock must be free again: a plain forward succeeds (the
    # FR-205 'next request clean' invariant, offline flavor).
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, _, _ = _post_stream(base, "/engines/llm/infer/stream", {"prompt": "hi"})
        assert code == 200
        code, body = _req(base, "/engines/llm/infer", "POST", {"prompt": "again"})
        assert code == 200 and body["text"] == "ok"
    finally:
        server.shutdown()


# -- control secret (unchanged posture on both transports) ---------------------------------------

def test_control_unload_secret_enforced_on_both_runtimes(runtime, monkeypatch):
    from hostagent import main as agent_main
    monkeypatch.setattr(agent_main, "CONTROL_SECRET", "s3kr1t")
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, body = _req(base, "/control/unload", "POST", {"engine": "llm"})
        assert code == 403 and "X-Agent-Control" in body["error"]
        code, body = _req(base, "/control/unload", "POST", {"engine": "llm"},
                          headers={"X-Agent-Control": "s3kr1t"})
        assert code in (200, 409)  # authed: idle/unloaded (or a refused drain) — not a 403
    finally:
        server.shutdown()


# -- multipart (vision-shaped) --------------------------------------------------------------------

class FakeVision:
    """A multipart engine (vision-shaped): forward_multipart, no JSON forward."""
    engine_id = "vision"
    gpu = True
    optional = False
    verbs = ("classify",)
    stream_verbs = ()

    def __init__(self):
        self._children = []

    def available(self):
        return (True, None)

    def estimate_vram(self):
        return 1.0

    def spawn(self):
        c = FakeChild(pid=6000 + len(self._children))
        self._children.append(c)
        return c

    def ready(self):
        return True

    def forward_multipart(self, verb, raw_body, content_type, load_ms):
        return {"model": "m", "device": "cpu", "predictions": [{"label": "cat", "score": 0.9}],
                "echo_len": len(raw_body), "echo_ctype": content_type}

    def health(self, resident):
        return {"ok": True, "resident": resident, "model": "m"}


def _serve_vision(runtime):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(store=FakeJobStore())
    rt = lifecycle.EngineRuntime(FakeVision(), admission)
    manager = lifecycle.EngineManager(admission, runtimes={"vision": rt})
    server = start_agent(admission, journal, manager,
                         jobs_mod.JobManager(admission, journal), runtime)
    return server, server.base_url


def _post_multipart(base, path, body):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=b"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read()) if e.headers.get("content-type", "").startswith(
            "application/json") else {})


def test_multipart_classify_relayed_to_vision_adapter(runtime):
    server, base = _serve_vision(runtime)
    try:
        body = b"--b\r\nContent-Disposition: form-data; name=\"image\"\r\n\r\nPNG\r\n--b--\r\n"
        code, out = _post_multipart(base, "/engines/vision/classify", body)
        assert code == 200 and out["predictions"][0]["label"] == "cat"
        assert out["echo_len"] == len(body) and out["echo_ctype"].startswith("multipart/")
    finally:
        server.shutdown()


def test_multipart_to_a_json_only_engine_is_415(runtime):
    # a JSON engine (no forward_multipart) must reject a multipart POST, not crash
    server, base, _ = _serve(FakeLLM(), runtime)
    try:
        code, _ = _post_multipart(base, "/engines/llm/infer", b"--b\r\n--b--\r\n")
        assert code == 415
    finally:
        server.shutdown()


def test_json_to_a_multipart_only_engine_is_415(runtime):
    # symmetric (claude review, T360): a JSON POST to a multipart-only engine (vision has no
    # `forward`) must be a clean 415, not an AttributeError -> 502
    server, base = _serve_vision(runtime)
    try:
        code, _ = _req(base, "/engines/vision/classify", "POST", {"image_b64": "x"})
        assert code == 415
    finally:
        server.shutdown()


# NOTE: the byte-compatible `/engines/<id>/unload-now` relic was retired at T364 (its callers, the
# gateway swap, went away at T363); operators use `/control/unload`. The stale 200-idle test that the
# T364 merge left behind — which now (correctly) 404s — was removed here.


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
