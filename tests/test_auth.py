"""Phase 1 auth integration test (T047, US1 / SC-008).

Asserts the gateway is closed to unauthenticated access once auth is enabled:
  - a protected endpoint (`GET /models`) returns 401 with NO key and with a WRONG key,
  - the same endpoint returns 200 with the VALID key,
  - `/healthz` and `/metrics` stay reachable WITHOUT a key (probes/Prometheus keep working).

Requires the stack up with auth enabled (GATEWAY_API_KEYS set on the gateway) and the valid key
exposed to the test as GATEWAY_API_KEY (scripts/gen_secrets prints both). SKIPs cleanly if no key
is configured for the test, so it never blocks an open dev run. Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
KEY = os.getenv("GATEWAY_API_KEY")
PROTECTED = "/models"  # read-only, no side effects


def _status(path, key=None, method="GET"):
    """Return the HTTP status for a request, treating HTTPError as its code (not an exception)."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(GW + path, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> int:
    if not KEY:
        print("[SKIP] GATEWAY_API_KEY not set — cannot exercise auth (run scripts/gen_secrets, "
              "then set GATEWAY_API_KEY). Open-mode gateways have nothing to assert here.")
        return 0

    failures = 0

    # 1. No key -> 401 on a protected endpoint.
    s = _status(PROTECTED)
    ok = s == 401
    print(f"[{'OK' if ok else 'FAIL'}] {PROTECTED} without a key -> {s} (expect 401)")
    failures += 0 if ok else 1

    # 2. Wrong key -> 401.
    s = _status(PROTECTED, key="mll_definitely-not-valid")
    ok = s == 401
    print(f"[{'OK' if ok else 'FAIL'}] {PROTECTED} with a wrong key -> {s} (expect 401)")
    failures += 0 if ok else 1

    # 3. Valid key -> 200.
    s = _status(PROTECTED, key=KEY)
    ok = s == 200
    print(f"[{'OK' if ok else 'FAIL'}] {PROTECTED} with the valid key -> {s} (expect 200)")
    failures += 0 if ok else 1

    # 4. Probes stay open without a key.
    for probe in ("/healthz", "/metrics"):
        s = _status(probe)
        ok = s == 200
        print(f"[{'OK' if ok else 'FAIL'}] {probe} unauthenticated -> {s} (expect 200)")
        failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} auth check(s) failed.")
        return 1
    print("\nT047 PASS — gateway closed to unauthenticated access; probes stay open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
