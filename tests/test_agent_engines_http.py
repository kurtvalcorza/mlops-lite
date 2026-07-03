"""018 T358 — the agent's /engines HTTP surface (offline, GPU-free).

A real ThreadingHTTPServer over a fake LLM adapter drives the routing added by the LLM fold-in and
its review fixes: per-engine health with a 503 when the engine can't serve (adapter-unavailable OR
runtime-wedged, Codex rounds 7/8), strict path-shape rejection of malformed inference paths (round
8), byte-compatible `unload-now` idle payload, and the happy-path forward.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from test_agent_lifecycle import FakeChild  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle  # noqa: E402
from hostagent import main as agent_main
from hostagent.journal import Journal  # noqa: E402


class FakeLLM:
    engine_id = "llm"
    gpu = True
    optional = False
    verbs = ("infer",)
    stream_verbs = ("infer",)

    def __init__(self):
        self.avail = (True, None)
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

    def health(self, resident):
        ok, reason = self.avail
        p = {"ok": ok, "resident": resident, "model": "m"}
        if not ok:
            p["unavailable"] = reason
        return p


def _serve(adapter):
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(os.path.join(tempfile.mkdtemp(prefix="agent-eng-"), "journal.jsonl"))
    rt = lifecycle.EngineRuntime(adapter, admission)
    manager = lifecycle.EngineManager(admission, runtimes={"llm": rt})
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(admission, journal, manager))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", rt


def _req(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def test_engine_health_200_when_serviceable():
    server, base, _ = _serve(FakeLLM())
    try:
        code, body = _req(base, "/engines/llm/health")
        assert code == 200 and body["ok"] is True and body["model"] == "m"
        code, _ = _req(base, "/engines/nope/health")
        assert code == 404
    finally:
        server.shutdown()


def test_engine_health_503_when_adapter_unavailable():
    a = FakeLLM()
    a.avail = (False, "model GGUF not found")
    server, base, _ = _serve(a)
    try:
        code, body = _req(base, "/engines/llm/health")
        assert code == 503 and body["ok"] is False and "model" in body["unavailable"]
    finally:
        server.shutdown()


def test_engine_health_503_when_runtime_wedged():
    # Codex round 8: a wedged child (survived SIGKILL, GPU pinned) reports ok:true from the
    # adapter's file checks alone — the route must fold in the runtime state and answer 503.
    server, base, rt = _serve(FakeLLM())
    try:
        rt.wedged_reason = "child pid=1 survived SIGKILL"
        code, body = _req(base, "/engines/llm/health")
        assert code == 503 and body["ok"] is False and "wedged" in body
    finally:
        server.shutdown()


def test_forward_happy_path():
    server, base, _ = _serve(FakeLLM())
    try:
        code, body = _req(base, "/engines/llm/infer", "POST", {"prompt": "hi"})
        assert code == 200 and body["text"] == "ok"
    finally:
        server.shutdown()


def test_strict_path_rejects_extra_segments():
    # Codex round 8: /engines/llm/infer/typo and /engines/llm/infer/stream/extra must 404, not
    # silently forward as the bare `infer` verb.
    server, base, _ = _serve(FakeLLM())
    try:
        for bad in ("/engines/llm/infer/typo", "/engines/llm/infer/stream/extra"):
            code, _ = _req(base, bad, "POST", {"prompt": "hi"})
            assert code == 404, f"{bad} should 404"
    finally:
        server.shutdown()


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


def _serve_vision():
    admission = adm.Admission(vram_budget_gb=12.0,
                              gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    journal = Journal(os.path.join(tempfile.mkdtemp(prefix="agent-vis-"), "journal.jsonl"))
    rt = lifecycle.EngineRuntime(FakeVision(), admission)
    manager = lifecycle.EngineManager(admission, runtimes={"vision": rt})
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_main.make_handler(admission, journal, manager))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post_multipart(base, path, body):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=b"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read()) if e.headers.get("content-type", "").startswith(
            "application/json") else {})


def test_multipart_classify_relayed_to_vision_adapter():
    server, base = _serve_vision()
    try:
        body = b"--b\r\nContent-Disposition: form-data; name=\"image\"\r\n\r\nPNG\r\n--b--\r\n"
        code, out = _post_multipart(base, "/engines/vision/classify", body)
        assert code == 200 and out["predictions"][0]["label"] == "cat"
        assert out["echo_len"] == len(body) and out["echo_ctype"].startswith("multipart/")
    finally:
        server.shutdown()


def test_multipart_to_a_json_only_engine_is_415():
    # a JSON engine (no forward_multipart) must reject a multipart POST, not crash
    server, base, _ = _serve(FakeLLM())
    try:
        code, _ = _post_multipart(base, "/engines/llm/infer", b"--b\r\n--b--\r\n")
        assert code == 415
    finally:
        server.shutdown()


def test_unload_now_idle_payload_is_byte_compatible():
    # not resident -> exactly {"status": "idle"} (no shared-lifecycle `drained` key)
    server, base, _ = _serve(FakeLLM())
    try:
        code, body = _req(base, "/engines/llm/unload-now", "POST", {"drain_timeout_s": 1})
        assert code == 200 and body == {"status": "idle"}
    finally:
        server.shutdown()


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
