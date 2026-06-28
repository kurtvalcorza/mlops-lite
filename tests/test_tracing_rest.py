"""006 US1 REST inference-tracing test (T107, FR-049 / SC-031).

With the stack + MLflow up and tracing enabled, one `POST /infer` produces exactly one MLflow trace in
the `mlops-lite-inference` experiment with the request's prompt captured (and, since capture_io is on by
default, the output). The response body/status are unchanged. SKIPs cleanly when the stack / key /
MLflow / tracing are absent.

  python tests/test_tracing_rest.py
"""
import json
import os
import sys
import time
import urllib.request

from _traces import trace_outputs, wait_for_trace

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
KEY = os.getenv("GATEWAY_API_KEY")


def _post(path, body):
    req = urllib.request.Request(
        GW + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": KEY or ""}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, json.load(r)


def main() -> int:
    failures = 0
    marker = f"rest-trace-{int(time.time())}"
    s, data = _post("/infer", {"prompt": f"{marker}: reply pong", "max_tokens": 8, "temperature": 0.1})
    ok = s == 200 and data.get("status") == "completed" and bool(data.get("text"))
    print(f"[{'OK' if ok else 'FAIL'}] /infer -> {s} (text={data.get('text', '')[:30]!r}) — body unchanged")
    failures += 0 if ok else 1

    t = wait_for_trace(marker, timeout=25)
    if not t:
        print("[FAIL] no MLflow trace appeared for the /infer request")
        return 1
    ok = t.get("status") == "OK"
    print(f"[{'OK' if ok else 'FAIL'}] trace recorded -> status={t.get('status')} "
          f"dur_ms={t.get('execution_time_ms')}")
    failures += 0 if ok else 1

    out = trace_outputs(t)
    has_out = out is not None and "output" in out
    print(f"[{'OK' if has_out else 'FAIL'}] trace captured output (capture_io on) -> {bool(has_out)}")
    failures += 0 if has_out else 1

    if failures:
        print(f"\n{failures} REST-tracing check(s) failed.")
        return 1
    print("\nT107 PASS — /infer emits one MLflow trace with prompt/output; response unchanged")
    return 0


def test_tracing_rest(require_gateway, require_key, require_mlflow, require_tracing):
    """Pytest wrapper (006 US1): needs the stack + key + MLflow + tracing on; else skip."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
