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


if __name__ == "__main__":
    sys.exit(main())
