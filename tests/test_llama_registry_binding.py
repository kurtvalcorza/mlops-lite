"""022 T465/T469 — the llama adapter's registry binding + honest identity (offline, GPU-free).

Pins: `rebind()` binds base/adapter/alias/version from the resolver before spawn (`-m base
--lora adapter --alias <registry name>`); the env knobs are the FALLBACK only (pointer unset, or
resolution infra unreachable — surfaced as `binding` in health, never silent); an unresolvable
target makes the engine unavailable (fail loud, FR-265 — the swap target-probe refuses); the
health payload carries the loaded model_name + registry_version (the agent is the identity
source of truth, FR-260); the forward/stream `model` field names the registry model (FR-262).
"""
import json
import os
import sys
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import serving_llm  # noqa: E402
from hostagent.adapters.llama import LlamaAdapter  # noqa: E402


def _files(tmp_path):
    bin_ = tmp_path / "llama-server"
    bin_.write_text("#!/bin/sh\n")
    os.chmod(bin_, 0o755)
    base = tmp_path / "base.gguf"
    base.write_bytes(b"b" * (2 * 1024 * 1024))
    adapter = tmp_path / "adapter.gguf"
    adapter.write_bytes(b"a" * 1024)
    return str(bin_), str(base), str(adapter)


def _adapter(tmp_path, monkeypatch, resolver, env_model=None):
    bin_, base, adapter = _files(tmp_path)
    monkeypatch.setenv("LLAMA_BIN", bin_)
    monkeypatch.setenv("MODEL", env_model or base)
    monkeypatch.delenv("LORA", raising=False)
    a = LlamaAdapter(resolver=resolver)
    return a, base, adapter


def _target(base, adapter=None, name="ops-bot", version="3"):
    return {"model_name": name, "version": version,
            "kind": "lora-adapter" if adapter else "full-model",
            "base_gguf": base, "adapter_gguf": adapter,
            "base": {"name": "qwen-base", "version": "1", "source": base} if adapter else None}


class FakePopen:
    def __init__(self, cmd):
        self.cmd, self.pid = cmd, 4242

    def poll(self):
        return None


def test_resolved_adapter_binds_spawn_args_and_identity(tmp_path, monkeypatch):
    a, base, adapter = _adapter(tmp_path, monkeypatch,
                                resolver=lambda: _target(None, None))
    a._resolver = lambda: _target(base, adapter)
    captured = {}
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: captured.update(cmd=cmd) or
                        FakePopen(cmd), raising=False)
    import hostagent.adapters.llama as llama_mod
    monkeypatch.setattr(llama_mod.subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd))
    assert a.available() == (True, None)
    child = a.spawn()
    cmd = child.cmd
    assert cmd[cmd.index("-m") + 1] == base
    assert cmd[cmd.index("--lora") + 1] == adapter
    assert cmd[cmd.index("--alias") + 1] == "ops-bot"
    assert a.bound_identity() == ("ops-bot", "3")
    assert a.loaded_identity() == ("ops-bot", "3")


def test_full_model_spawn_has_no_lora_flag(tmp_path, monkeypatch):
    a, base, _ = _adapter(tmp_path, monkeypatch, resolver=None)
    a._resolver = lambda: _target(base, None, name="qwen-base", version="1")
    import hostagent.adapters.llama as llama_mod
    monkeypatch.setattr(llama_mod.subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd))
    a.rebind(force=True)
    child = a.spawn()
    assert "--lora" not in child.cmd and child.cmd[child.cmd.index("-m") + 1] == base


def test_unset_pointer_falls_back_to_env_defaults(tmp_path, monkeypatch):
    a, base, _ = _adapter(tmp_path, monkeypatch, resolver=lambda: None)
    a.rebind(force=True)
    assert a.model == base and a.lora is None
    assert a.alias == a._env_alias and a.registry_version is None
    h = a.health(resident=False)
    assert "binding" not in h  # a clean default is not a degradation


def test_unreachable_resolution_falls_back_and_surfaces_note(tmp_path, monkeypatch):
    def resolver():
        raise serving_llm.ResolutionUnavailable("store down")

    a, base, _ = _adapter(tmp_path, monkeypatch, resolver=resolver)
    assert a.available() == (True, None)  # env default still serves (today's behavior)
    h = a.health(resident=False)
    assert "binding" in h and "store down" in h["binding"]
    assert h["model_name"] == a._env_alias and h["registry_version"] is None


def test_unresolvable_target_makes_engine_unavailable(tmp_path, monkeypatch):
    def resolver():
        raise serving_llm.ResolutionError("base 'x' is not registered")

    a, _, _ = _adapter(tmp_path, monkeypatch, resolver=resolver)
    ok, reason = a.available()
    assert not ok and "unresolvable" in reason and "base 'x'" in reason


def test_missing_bound_adapter_file_is_unavailable(tmp_path, monkeypatch):
    a, base, _ = _adapter(tmp_path, monkeypatch, resolver=None)
    a._resolver = lambda: _target(base, str(tmp_path / "gone-adapter.gguf"))
    ok, reason = a.available()
    assert not ok and "LoRA adapter" in reason


def test_rebind_is_ttl_cached_and_force_busts_it(tmp_path, monkeypatch):
    calls = []

    def resolver():
        calls.append(1)
        return None

    a, _, _ = _adapter(tmp_path, monkeypatch, resolver=resolver)
    a.available()
    a.available()
    assert len(calls) == 1          # second available() within the TTL — no new resolve
    a.rebind(force=True)
    assert len(calls) == 2          # the reload verb busts the cache


def test_health_reports_loaded_identity_when_resident(tmp_path, monkeypatch):
    a, base, adapter = _adapter(tmp_path, monkeypatch, resolver=None)
    a._resolver = lambda: _target(base, adapter)
    import hostagent.adapters.llama as llama_mod
    monkeypatch.setattr(llama_mod.subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd))
    a.rebind(force=True)
    a.spawn()
    h = a.health(resident=True)
    assert h["model"] == "ops-bot" and h["model_name"] == "ops-bot"
    assert h["registry_version"] == "3"
    assert h["base"] == os.path.basename(base) and h["adapter"] == os.path.basename(adapter)


def test_forward_and_stream_echo_the_registry_identity(tmp_path, monkeypatch):
    a, base, adapter = _adapter(tmp_path, monkeypatch, resolver=None)
    a._resolver = lambda: _target(base, adapter)
    import hostagent.adapters.llama as llama_mod
    monkeypatch.setattr(llama_mod.subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd))
    a.rebind(force=True)
    a.spawn()
    a._port = 9999

    body = json.dumps({"choices": [{"message": {"content": "hi"}}], "usage": {}}).encode()

    class _Resp:
        status = 200

        def read(self, *a):
            return body

        def __iter__(self):
            return iter([b'data: [DONE]\n'])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    out = a.forward("infer", {"prompt": "x"}, 0.0)
    assert out["model"] == "ops-bot"  # FR-262: the response names the registry model
    frames = list(a.stream("infer", {"prompt": "x"}, 0.0))
    assert b'"model": "ops-bot"' in frames[0] and b'"model": "ops-bot"' in frames[-1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
