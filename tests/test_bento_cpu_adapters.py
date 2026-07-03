"""018 T361 — embed + tabular CPU engine adapters (offline, GPU-free).

Both are thin subclasses of the shared BentoCpuAdapter. Stubs `urllib.request.urlopen` so no bento
runs; pins the interface, the JSON forward passthrough to the child endpoint, the opt-in
`unavailable` when bentoml is absent, and that CPU engines are registered off-lease (gpu=False) and
never idle-reaped.
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
from hostagent.adapters.embed import EmbedAdapter  # noqa: E402
from hostagent.adapters.tabular import TabularAdapter  # noqa: E402


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _installed(tmp_path, monkeypatch):
    binroot = tmp_path / "venv" / "bin"
    binroot.mkdir(parents=True)
    bento = binroot / "bentoml"
    bento.write_text("#!/bin/sh\n")
    os.chmod(bento, 0o755)
    monkeypatch.setenv("VENV", str(tmp_path / "venv"))


def test_embed_metadata():
    a = EmbedAdapter()
    assert a.engine_id == "embed" and a.gpu is False and a.optional is False
    assert a.verbs == ("embed",) and a.stream_verbs == () and a.estimate_vram() == 0.0


def test_tabular_metadata():
    a = TabularAdapter()
    assert a.engine_id == "tabular" and a.gpu is False
    assert a.verbs == ("predict",)


def test_embed_available_unavailable_without_bentoml(tmp_path, monkeypatch):
    monkeypatch.setenv("VENV", str(tmp_path / "nope"))
    ok, reason = EmbedAdapter().available()
    assert not ok and "bentoml" in reason


def test_embed_forward_relays_json_and_returns_vectors(tmp_path, monkeypatch):
    _installed(tmp_path, monkeypatch)
    a = EmbedAdapter()
    a._port = 9001
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp([[0.1, 0.2], [0.3, 0.4]])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = a.forward("embed", {"texts": ["a", "b"]}, load_ms=3.0)
    assert out == [[0.1, 0.2], [0.3, 0.4]]                 # vectors returned verbatim
    assert captured["url"].endswith("/embed")              # POSTed to the child's /embed
    assert captured["body"] == {"texts": ["a", "b"]}       # body relayed unchanged


def test_tabular_forward_relays_json_and_returns_dict(tmp_path, monkeypatch):
    _installed(tmp_path, monkeypatch)
    a = TabularAdapter()
    a._port = 9002
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp({"predictions": [1, 0]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = a.forward("predict", {"rows": [{"x": 1}, {"x": 2}]}, load_ms=1.0)
    assert out == {"predictions": [1, 0]}
    assert captured["url"].endswith("/predict")
    assert captured["body"] == {"rows": [{"x": 1}, {"x": 2}]}


def test_forward_rejects_unknown_verb(tmp_path, monkeypatch):
    _installed(tmp_path, monkeypatch)
    a = EmbedAdapter()
    a._port = 9003
    with pytest.raises(ValueError):
        a.forward("predict", {"texts": []}, 0.0)


def test_health_is_cpu_shaped(tmp_path, monkeypatch):
    _installed(tmp_path, monkeypatch)
    h = EmbedAdapter().health(resident=True)
    assert h["ok"] is True and h["engine"] == "embed" and h["device"] == "cpu"
    assert "vram_free_gb" not in h  # CPU engines carry no VRAM fields


def test_build_runtimes_registers_cpu_engines_off_lease_and_no_idle_reap(tmp_path):
    from hostagent import admission as adm

    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0))
    rts = adapters.build_runtimes(a)
    for eid in ("embed", "tabular"):
        assert eid in rts and rts[eid].adapter.gpu is False
        # CPU engines are never idle-reaped (no VRAM to free)
        assert rts[eid].idle_timeout_s == float("inf")
    # a GPU engine keeps a finite idle timeout
    assert rts["llm"].idle_timeout_s != float("inf")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
