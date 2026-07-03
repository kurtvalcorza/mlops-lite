"""Phase 3 serving smoke test (T018, US1).

Verifies on-demand inference works through the gateway. Requires the stack up (serve_up.ps1)
and a native llama-server running. Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"


def _get(path):
    req = urllib.request.Request(GW + path, headers=auth_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _post(path, body):
    req = urllib.request.Request(
        GW + path,
        data=json.dumps(body).encode(),
        headers=auth_headers({"Content-Type": "application/json"}),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, json.load(r)


def main() -> int:
    health = _get("/serving/health")
    if not health.get("reachable"):
        print(f"[FAIL] serving backend not reachable: {health}")
        return 1
    print(f"[OK] serving backend reachable ({health.get('backend')})")

    status, data = _post("/infer", {"prompt": "Reply with the single word: pong", "max_tokens": 8})
    ok = status == 200 and data.get("status") == "completed" and bool(data.get("text"))
    print(f"[{'OK' if ok else 'FAIL'}] /infer -> {data.get('text', '')[:60]!r} "
          f"({data.get('infer_ms')} ms, model={data.get('model')})")
    return 0 if ok else 1


def test_serving(require_serving, require_key):
    """Pytest wrapper (005 US5): skip unless the serving daemon is reachable + a key is set."""
    assert main() == 0


# --- 018 US1 (FR-167): llama reaps a stuck child before relaunching (offline) ----------------------
#
# Regression for the duplication drift the 2026-07 architecture review flagged (§4.2): whisper
# gained reap-before-relaunch in 016's review; the llama copy didn't. A resident-but-unready child
# must be _unload()ed BEFORE a fresh lease acquire + Popen — otherwise the new child hits the
# orphan's fixed port (EADDRINUSE) and the failure path releases the lease the orphan still uses.

import importlib.util as _ilu

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_llama_supervisor():
    path = os.path.join(_REPO, "serving", "llama", "supervisor.py")
    spec = _ilu.spec_from_file_location("llama_supervisor_reap_test", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_llama_reaps_stuck_child_before_relaunch():
    mod = _load_llama_supervisor()
    state = {"resident": True}
    events = []
    mod._resident = lambda: state["resident"]
    mod._server_ready = lambda: False           # resident but NOT ready — the stuck-child case
    mod._estimate_vram_gb = lambda: 1.0         # no model file in the offline environment

    def _unload():
        events.append("unload")
        state["resident"] = False

    mod._unload = _unload

    class _Stop(Exception):
        pass

    def _acquire(*a, **kw):
        events.append("acquire")
        raise _Stop()                           # stop the flow after the ordering is observable

    orig_acquire = mod.gpu_lease.acquire        # gpu_lease is a shared real module — restore it
    mod.gpu_lease.acquire = _acquire
    try:
        try:
            mod._ensure_loaded()
        except _Stop:
            pass
        assert events == ["unload", "acquire"], events  # reap FIRST, then a fresh acquire
    finally:
        mod.gpu_lease.acquire = orig_acquire


if __name__ == "__main__":
    sys.exit(main())
