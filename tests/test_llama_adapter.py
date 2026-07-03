"""018 T358 — LLM engine adapter unit tests (offline, GPU-free).

Stubs `urllib.request.urlopen` so no llama-server runs; pins the adapter interface, the
byte-compatible forward/response shapes, and the SSE frames the gateway's raw proxy scans for
(`"event": "done"`). Also pins that adding the engine is one registry row (`build_runtimes`).
"""
import json
import os
import sys
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import adapters  # noqa: E402
from hostagent.adapters.llama import LlamaAdapter  # noqa: E402


class _Resp:
    """Context-manager fake for a urllib response: a JSON body (forward), a line iterator
    (stream), or a health 200."""

    def __init__(self, *, status=200, json_body=None, lines=None):
        self.status = status
        self._json = json_body
        self._lines = lines or []

    def read(self):
        return json.dumps(self._json).encode()

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _adapter(tmp_path, monkeypatch, *, with_model=True):
    if with_model:
        bin_ = tmp_path / "llama-server"
        bin_.write_text("#!/bin/sh\n")
        model = tmp_path / "model.gguf"
        model.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MiB — a nonzero size for the estimate
        monkeypatch.setenv("LLAMA_BIN", str(bin_))
        monkeypatch.setenv("MODEL", str(model))
    else:
        monkeypatch.setenv("LLAMA_BIN", str(tmp_path / "nope-bin"))
        monkeypatch.setenv("MODEL", str(tmp_path / "nope-model"))
    a = LlamaAdapter()
    a._port = 9999  # pretend a child is up on this port
    return a


def test_available_reports_missing_prereqs(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, with_model=False)
    ok, reason = a.available()
    assert not ok and "llama-server" in reason


def test_available_ok_and_estimate_positive(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a.available() == (True, None)
    assert a.estimate_vram() > 1.0  # size*1.2 + 1.0 headroom


def test_estimate_missing_file_is_near_budget_not_zero(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, with_model=False)
    # never fail open: a missing model must NOT estimate 0 and get admitted for free
    assert a.estimate_vram() == pytest.approx(a.vram_budget_gb * 0.95)


def test_forward_builds_chat_payload_and_shapes_response(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp(json_body={"choices": [{"message": {"content": "hi there"}}],
                                "usage": {"total_tokens": 5}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = a.forward("infer", {"prompt": "hello", "max_tokens": 8, "temperature": 0.0}, load_ms=12.3)
    assert out["text"] == "hi there"
    assert out["load_ms"] == 12.3 and out["model"] == a.alias
    assert out["usage"] == {"total_tokens": 5} and "infer_ms" in out
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["messages"][0]["content"] == "hello"
    assert captured["body"]["max_tokens"] == 8 and captured["body"]["temperature"] == 0.0


def test_forward_rejects_unknown_verb(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        a.forward("classify", {}, 0.0)


def test_stream_emits_byte_compatible_frames(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    lines = [b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
             b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
             b'data: [DONE]\n']
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp(lines=lines))
    frames = list(a.stream("infer", {"prompt": "x"}, load_ms=1.0))
    joined = b"".join(frames)
    assert frames[0].startswith(b"data: ") and b'"event": "start"' in frames[0]
    assert b'"event": "token"' in joined and b"Hel" in joined and b"lo" in joined
    assert b'"event": "done"' in frames[-1]   # the exact marker gateway/app/routers/stream.py scans
    assert frames[-1].endswith(b"\n\n")


def test_health_shape_byte_compatible(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)

    class FakeLease:
        def current_holder(self):
            return {"tenant": "vision"}   # a DIFFERENT tenant holds the global lockfile

        def free_vram_gb(self):
            return 7.2

    a._lease = FakeLease()
    h = a.health(resident=True)
    assert h["ok"] is True and h["resident"] is True and h["model"] == a.alias
    assert h["lease_holder"] == "vision" and h["vram_free_gb"] == 7.2 and h["fits"] is True
    assert set(h) >= {"ok", "resident", "model", "vram_budget_gb", "est_vram_gb", "fits",
                      "vram_free_gb", "lease_holder"}


def test_health_without_lease_reports_no_holder(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)  # lease=None
    h = a.health(resident=False)
    assert h["lease_holder"] is None and h["vram_free_gb"] is None and h["resident"] is False


def test_health_ok_false_when_prereqs_missing(tmp_path, monkeypatch):
    # Codex round 7: an unavailable engine must NOT report ok:true, or the gateway swap target-probe
    # (any 200 = reachable) would evict a working holder for an LLM request that then fails to load.
    a = _adapter(tmp_path, monkeypatch, with_model=False)
    h = a.health(resident=False)
    assert h["ok"] is False and "llama-server" in h["unavailable"]


def test_build_runtimes_honors_idle_timeout_env(tmp_path, monkeypatch):
    # Codex round 7: the retired supervisor read IDLE_TIMEOUT; a fold-in that only flips the gateway
    # URL must not silently reset a tuned deployment's idle timeout to the 120s default.
    from hostagent import admission as adm

    monkeypatch.setenv("IDLE_TIMEOUT", "45")
    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    rts = adapters.build_runtimes(a)
    assert rts["llm"].idle_timeout_s == 45.0


def test_build_runtimes_registers_llm_engine(tmp_path, monkeypatch):
    from hostagent import admission as adm

    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    rts = adapters.build_runtimes(a)
    assert "llm" in rts
    assert rts["llm"].adapter.engine_id == "llm" and rts["llm"].adapter.gpu is True
    assert rts["llm"].kind == "serving"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
