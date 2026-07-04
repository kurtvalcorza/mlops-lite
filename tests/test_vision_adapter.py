"""018 T360 — Vision (BentoML) engine adapter unit tests (offline, GPU-free).

Stubs `urllib.request.urlopen` so no bento runs; pins the adapter interface, the multipart classify
passthrough (raw body + Content-Type relayed unchanged), the opt-in `unavailable` when bentoml is
absent, and that `_ProcGroup.terminate()` reaps the whole process group (BentoML forks workers).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import adapters  # noqa: E402
from hostagent.adapters._common import ProcGroup  # noqa: E402
from hostagent.adapters.vision import VisionAdapter  # noqa: E402


class _Resp:
    def __init__(self, *, status=200, json_body=None):
        self.status = status
        self._json = json_body if json_body is not None else {"predictions": []}

    def read(self):
        return json.dumps(self._json).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _adapter(tmp_path, monkeypatch, *, installed=True):
    if installed:
        binroot = tmp_path / "venv" / "bin"
        binroot.mkdir(parents=True)
        bento = binroot / "bentoml"
        bento.write_text("#!/bin/sh\n")
        os.chmod(bento, 0o755)
        monkeypatch.setenv("VENV", str(tmp_path / "venv"))
    else:
        monkeypatch.setenv("VENV", str(tmp_path / "empty-venv"))
    a = VisionAdapter()
    a._port = 9000
    return a


def test_engine_metadata():
    a = VisionAdapter()
    assert a.engine_id == "vision" and a.gpu is True and a.optional is False
    assert a.verbs == ("classify",) and a.stream_verbs == ()


def test_available_unavailable_without_bentoml(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch, installed=False)
    ok, reason = a.available()
    assert not ok and "bentoml" in reason


def test_available_ok_when_installed(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    assert a.available() == (True, None)
    assert a.estimate_vram() == a.est_gb


def test_forward_multipart_relays_raw_body_to_classify(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["ctype"] = req.headers.get("Content-type")
        captured["body"] = req.data
        return _Resp(json_body={"model": "vision-mobilenet", "device": "cuda",
                                "predictions": [{"label": "tabby cat", "score": 0.91}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = b'--b\r\nContent-Disposition: form-data; name="image"\r\n\r\nPNGDATA\r\n--b--\r\n'
    ctype = "multipart/form-data; boundary=b"
    out = a.forward_multipart("classify", body, ctype, load_ms=5.0)
    assert out["predictions"][0]["label"] == "tabby cat" and out["device"] == "cuda"
    # relayed verbatim — same bytes, same Content-Type, to /classify
    assert captured["url"].endswith("/classify")
    assert captured["ctype"] == ctype and captured["body"] == body


def test_forward_multipart_rejects_unknown_verb(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        a.forward_multipart("infer", b"x", "multipart/form-data; boundary=b", 0.0)


def test_ready_probes_readyz(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["url"] = url
        return _Resp(status=200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert a.ready() is True and seen["url"].endswith("/readyz")


def test_health_shape(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)

    class FakeAdmission:
        def holder(self):
            return {"tenant": "llm"}

        def free_gb(self):
            return 4.0

    a._admission = FakeAdmission()   # T364: health reads admission (was the lockfile lease)
    h = a.health(resident=True)
    assert h["ok"] is True and h["model"] == a.model_name and h["lease_holder"] == "llm"


def test_procgroup_terminate_reaps_the_group():
    # BentoML forks workers, so the child is spawned in its own session and terminate() must signal
    # the whole group. Drive it with a real `sleep` child (WSL/Linux only — killpg is POSIX).
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pg = ProcGroup(p)
    assert pg.pid == p.pid and pg.poll() is None
    pg.terminate()
    for _ in range(60):
        if pg.poll() is not None:
            break
        time.sleep(0.05)
    assert pg.poll() is not None


def test_build_runtimes_registers_vision_engine(tmp_path):
    from hostagent import admission as adm

    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    rts = adapters.build_runtimes(a)
    assert "vision" in rts and rts["vision"].adapter.engine_id == "vision"
    assert rts["vision"].adapter.gpu is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
