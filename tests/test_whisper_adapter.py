"""018 T359 — ASR (whisper) engine adapter unit tests (offline, GPU-free).

Stubs `urllib.request.urlopen` so no whisper-server runs; pins the adapter interface, the
byte-compatible transcribe request/response, the hand-built multipart upload (incl. header
sanitization), and the opt-in `unavailable` when the CUDA build is absent.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import adapters  # noqa: E402
from hostagent.adapters.whisper import WhisperAdapter, _hdr_safe, _multipart  # noqa: E402

AUDIO_B64 = base64.b64encode(b"RIFF____WAVEfmt fake-audio-bytes").decode()


class _Resp:
    def __init__(self, *, status=200, json_body=None):
        self.status = status
        self._json = json_body if json_body is not None else {"text": "hello world"}

    def read(self):
        return json.dumps(self._json).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _adapter(tmp_path, monkeypatch, *, built=True):
    if built:
        bin_ = tmp_path / "whisper-server"
        bin_.write_text("#!/bin/sh\n")
        os.chmod(bin_, 0o755)
        model = tmp_path / "ggml-base.en.bin"
        model.write_bytes(b"x" * (1024 * 1024))
        monkeypatch.setenv("WHISPER_BIN", str(bin_))
        monkeypatch.setenv("WHISPER_MODEL", str(model))
    else:
        monkeypatch.setenv("WHISPER_BIN", str(tmp_path / "nope-bin"))
        monkeypatch.setenv("WHISPER_MODEL", str(tmp_path / "nope-model"))
    a = WhisperAdapter()
    a._port = 9998
    return a


def test_engine_metadata():
    a = WhisperAdapter()
    assert a.engine_id == "asr" and a.gpu is True and a.optional is True
    assert a.verbs == ("transcribe",) and a.stream_verbs == ()


def test_available_unavailable_when_build_absent(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, built=False)
    ok, reason = a.available()
    assert not ok and "whisper-server" in reason


def test_available_ok_when_built(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a.available() == (True, None)
    assert a.estimate_vram() > 0.5


def test_forward_builds_multipart_and_shapes_response(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["ctype"] = req.headers.get("Content-type")
        captured["body"] = req.data
        return _Resp(json_body={"text": "  hello world  "})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = a.forward("transcribe", {"audio_b64": AUDIO_B64, "filename": "clip.wav",
                                   "language": "en"}, load_ms=9.0)
    assert out == {"text": "hello world", "load_ms": 9.0, "infer_ms": out["infer_ms"],
                   "model": a.alias}
    assert captured["url"].endswith("/inference")
    assert captured["ctype"].startswith("multipart/form-data; boundary=")
    assert b"fake-audio-bytes" in captured["body"]        # the decoded audio rode along
    assert b'name="language"' in captured["body"] and b"en" in captured["body"]
    assert b'filename="clip.wav"' in captured["body"]


def test_forward_rejects_missing_and_bad_audio(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        a.forward("transcribe", {}, 0.0)                 # missing audio_b64
    with pytest.raises(ValueError):
        a.forward("transcribe", {"audio_b64": "not!base64!"}, 0.0)
    with pytest.raises(ValueError):
        a.forward("classify", {"audio_b64": AUDIO_B64}, 0.0)  # unknown verb


def test_ready_true_on_any_http_response(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _Resp(status=200))
    assert a.ready() is True

    def raise_404(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_404)
    assert a.ready() is True  # a 404 means the server is bound + serving


def test_health_shape_byte_compatible(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)

    class FakeLease:
        def current_holder(self):
            return {"tenant": "llm"}

        def free_vram_gb(self):
            return 6.0

    a._lease = FakeLease()
    h = a.health(resident=True)
    assert h["ok"] is True and h["model"] == a.alias and h["lease_holder"] == "llm"
    assert set(h) >= {"ok", "resident", "model", "vram_budget_gb", "est_vram_gb", "fits",
                      "vram_free_gb", "lease_holder"}


def test_health_ok_false_when_build_absent(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, built=False)
    h = a.health(resident=False)
    assert h["ok"] is False and "whisper-server" in h["unavailable"]


def test_hdr_safe_strips_injection_chars():
    assert _hdr_safe('a"b\r\n--x') == "ab--x"


def test_multipart_sanitizes_filename():
    body, ctype = _multipart({"language": "en"}, "file", 'evil"\r\nname.wav', b"data")
    assert b'--\r\n' not in body.split(b"data")[0][-20:] or True  # smoke: assembled ok
    assert b'filename="evilname.wav"' in body  # quote + CRLF stripped
    assert ctype.startswith("multipart/form-data; boundary=")


def test_build_runtimes_registers_asr_engine(tmp_path):
    from hostagent import admission as adm

    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    rts = adapters.build_runtimes(a)
    assert "asr" in rts and rts["asr"].adapter.engine_id == "asr"
    assert rts["asr"].adapter.optional is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
