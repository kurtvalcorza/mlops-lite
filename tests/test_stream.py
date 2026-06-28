"""003 US1 streaming integration test (T068, FR-026/FR-027 / SC-013–014).

Asserts the gateway's SSE inference path behaves like its REST counterpart:
  - `POST /infer/stream` returns 401 with NO key and with a WRONG key (auth parity, FR-027),
  - with the VALID key it emits incremental SSE frames: a `start`, at least one `token`, a `done`,
  - the API key never appears in any streamed frame (SC-014 — key stays server-side).

Requires the stack up (gateway + native serving supervisor reachable) and auth enabled, with the
valid key exposed as GATEWAY_API_KEY (scripts/gen_secrets prints it). SKIPs cleanly if no key is
configured or the serving backend is unreachable, so it never blocks an open or infra-only dev run.
Exits non-zero on failure. A cold model load can take tens of seconds — the read timeout is generous.
"""
import json
import os
import sys
import urllib.error
import urllib.request

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
KEY = os.getenv("GATEWAY_API_KEY")
STREAM = "/infer/stream"
BODY = {"prompt": "Reply with exactly: pong", "max_tokens": 24, "temperature": 0.1}
TIMEOUT = float(os.getenv("STREAM_TEST_TIMEOUT", "150"))  # cold load + generation


def _status(key=None):
    """POST to the stream endpoint and return just the HTTP status (HTTPError -> its code)."""
    data = json.dumps(BODY).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(GW + STREAM, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r
    except urllib.error.HTTPError as e:
        return e.code, None


def _collect_events(key):
    """Stream the SSE response with a valid key; return the list of parsed event dicts."""
    data = json.dumps(BODY).encode()
    req = urllib.request.Request(
        GW + STREAM, data=data,
        headers={"Content-Type": "application/json", "X-API-Key": key}, method="POST")
    events, raw = [], []
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        for line in r:
            s = line.decode("utf-8", "ignore").strip()
            if not s.startswith("data:"):
                continue
            payload = s[5:].strip()
            raw.append(payload)
            try:
                events.append(json.loads(payload))
            except Exception:
                pass
    return events, raw


def main() -> int:
    if not KEY:
        print("[SKIP] GATEWAY_API_KEY not set — cannot exercise the authenticated stream "
              "(run scripts/gen_secrets, then set GATEWAY_API_KEY).")
        return 0

    # Probe: if serving isn't reachable the gateway 503s the stream — skip rather than fail (the
    # SSE plumbing is what we test here, not whether a GPU is currently attached).
    code, _ = _status(key=KEY)
    if code == 503:
        print("[SKIP] serving backend (supervisor) not reachable — bring the native daemons up "
              "(scripts/supervisor_up.sh) to exercise the live stream.")
        return 0

    failures = 0

    # 1. No key -> 401 (auth parity with REST /infer).
    s, _ = _status()
    ok = s == 401
    print(f"[{'OK' if ok else 'FAIL'}] {STREAM} without a key -> {s} (expect 401)")
    failures += 0 if ok else 1

    # 2. Wrong key -> 401.
    s, _ = _status(key="mll_definitely-not-valid")
    ok = s == 401
    print(f"[{'OK' if ok else 'FAIL'}] {STREAM} with a wrong key -> {s} (expect 401)")
    failures += 0 if ok else 1

    # 3. Valid key -> incremental SSE: start -> token+ -> done.
    events, raw = _collect_events(KEY)
    kinds = [e.get("event") for e in events]
    has_start = kinds[:1] == ["start"]
    has_token = "token" in kinds
    has_done = "done" in kinds
    print(f"[{'OK' if has_start else 'FAIL'}] first frame is 'start' -> {kinds[:1]}")
    print(f"[{'OK' if has_token else 'FAIL'}] at least one 'token' frame -> "
          f"{kinds.count('token')} token(s)")
    print(f"[{'OK' if has_done else 'FAIL'}] terminal 'done' frame present -> {has_done}")
    failures += sum(0 if c else 1 for c in (has_start, has_token, has_done))

    # 4. The API key must NOT appear anywhere in the streamed payload (SC-014).
    leaked = any(KEY in r for r in raw)
    print(f"[{'OK' if not leaked else 'FAIL'}] API key absent from the SSE stream -> "
          f"{'leaked!' if leaked else 'clean'}")
    failures += 1 if leaked else 0

    if failures:
        print(f"\n{failures} stream check(s) failed.")
        return 1
    print("\nT068 PASS — SSE stream is auth-gated, incremental (start/token/done), key never leaked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
