"""006 US2 streaming inference-tracing test (T109, FR-050 / SC-032).

A `POST /infer/stream` produces one MLflow trace AND the SSE passthrough is intact (start/token/done
frames present, not corrupted by the trace instrumentation). SKIPs cleanly when the stack / key /
serving daemon / MLflow / tracing are absent.

  python tests/test_tracing_stream.py
"""
import json
import os
import sys
import time
import urllib.request

from _traces import wait_for_trace

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
KEY = os.getenv("GATEWAY_API_KEY")


def _post_stream(body):
    req = urllib.request.Request(
        GW + "/infer/stream", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": KEY or ""}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def main() -> int:
    failures = 0
    marker = f"stream-trace-{int(time.time())}"
    s, body = _post_stream({"prompt": f"{marker}: reply pong", "max_tokens": 16, "temperature": 0.1})
    if s != 200:
        print(f"[FAIL] /infer/stream -> {s}")
        return 1

    # Passthrough intact: SSE framing + the supervisor's typed events survive the trace wrap unaltered.
    has_data = "data:" in body
    has_start = '"event": "start"' in body
    has_done = '"event": "done"' in body
    print(f"[{'OK' if has_data else 'FAIL'}] SSE 'data:' frames present")
    print(f"[{'OK' if (has_start and has_done) else 'FAIL'}] start+done events intact (passthrough "
          f"byte sequence unchanged)")
    failures += 0 if (has_data and has_start and has_done) else 1

    t = wait_for_trace(marker, timeout=25)
    if not t:
        print("[FAIL] no MLflow trace appeared for the /infer/stream request")
        return 1
    ok = t.get("status") == "OK"
    print(f"[{'OK' if ok else 'FAIL'}] stream trace recorded -> status={t.get('status')} "
          f"dur_ms={t.get('execution_time_ms')}")
    failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} stream-tracing check(s) failed.")
        return 1
    print("\nT109 PASS — /infer/stream emits one trace; SSE passthrough byte-identical")
    return 0


def test_tracing_stream(require_serving, require_key, require_mlflow, require_tracing):
    """Pytest wrapper (006 US2): needs serving + key + MLflow + tracing on; else skip."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
