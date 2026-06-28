"""003 cross-cutting security test (T077, SC-014/SC-015). Run in WSL.

Two guarantees the operator console must hold:
  1. **Key never browser-visible (SC-014/FR-024).** The gateway API key must not appear in any page
     HTML, in `__NEXT_DATA__`, or in any served JS chunk — the BFF holds it server-side. We fetch
     every page + every referenced `/_next/static/*.js` bundle and assert the key is absent.
  2. **Localhost-bound (SC-015/FR-025).** The UI must answer on 127.0.0.1 but NOT on the WSL LAN IP
     (eth0) — proving `next start -H 127.0.0.1` is not exposed to the network. No TLS, no LAN.

SKIPs cleanly if the UI or key isn't available. Exits non-zero on failure.
"""
import os
import re
import socket
import sys
import urllib.error
import urllib.request

UI = f"http://127.0.0.1:{os.getenv('UI_PORT', '3000')}"
KEY = os.getenv("GATEWAY_API_KEY")
PAGES = ["/", "/infer", "/models", "/datasets", "/runs", "/monitor", "/health"]


def _get(url, timeout=10):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def _req(url, method="GET", headers=None, timeout=10):
    """Request with a custom method/headers; returns (status, body, response-headers)."""
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), dict(e.headers)


def _eth0_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet sent; just picks the egress iface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main() -> int:
    try:
        s, _ = _get(f"{UI}/healthz", timeout=3)
        if s != 200:
            raise RuntimeError
    except Exception:
        print("[SKIP] UI not reachable on :3000 — start it (bash ui/run.sh / up_all).")
        return 0
    if not KEY:
        print("[SKIP] GATEWAY_API_KEY not set — nothing to assert about key leakage.")
        return 0

    failures = 0

    # 1. Collect page HTML + every referenced JS bundle, assert the key is in none of them.
    scanned, bundles = 0, set()
    for p in PAGES:
        s, html = _get(UI + p)
        scanned += 1
        if KEY in html:
            print(f"[FAIL] API key found in HTML of {p}")
            failures += 1
        for m in re.findall(r'/_next/static/[^"\']+\.js', html):
            bundles.add(m)

    for b in sorted(bundles):
        s, js = _get(UI + b)
        scanned += 1
        if s == 200 and KEY in js:
            print(f"[FAIL] API key found in bundle {b}")
            failures += 1
    print(f"[{'OK' if failures == 0 else 'FAIL'}] key absent from {scanned} browser-visible "
          f"payloads ({len(bundles)} JS bundles scanned)")

    # 2. Reachable on 127.0.0.1 ...
    s, _ = _get(f"{UI}/healthz")
    ok = s == 200
    print(f"[{'OK' if ok else 'FAIL'}] UI answers on 127.0.0.1 -> {s}")
    failures += 0 if ok else 1

    # ... but NOT on the LAN (eth0) IP — connection should be refused.
    ip = _eth0_ip()
    if not ip or ip.startswith("127."):
        print("[SKIP] could not determine a non-loopback IP to test LAN exposure.")
    else:
        try:
            s, _ = _get(f"http://{ip}:{os.getenv('UI_PORT', '3000')}/healthz", timeout=3)
            print(f"[FAIL] UI is reachable on the LAN IP {ip} -> {s} (must be 127.0.0.1-only)")
            failures += 1
        except Exception:
            print(f"[OK] UI NOT reachable on LAN IP {ip} (127.0.0.1-bound only)")

    # ---- 004 US1: BFF allowlist + origin guard + security headers ----

    # 3. Allowlist: an allowlisted route succeeds; a non-allowlisted path/method is refused by the
    #    BFF ITSELF (404 with its own "not proxied" body) — proving the key was never forwarded.
    s, body, _ = _req(f"{UI}/api/gw/models")
    ok = s == 200
    print(f"[{'OK' if ok else 'FAIL'}] BFF allowlisted GET /models -> {s} (expect 200)")
    failures += 0 if ok else 1

    s, body, _ = _req(f"{UI}/api/gw/admin/secrets")  # not on the allowlist
    ok = s == 404 and "not proxied" in body
    print(f"[{'OK' if ok else 'FAIL'}] BFF non-allowlisted path -> {s} (expect 404, refused by BFF "
          f"before key injection)")
    failures += 0 if ok else 1

    s, body, _ = _req(f"{UI}/api/gw/models", method="DELETE")  # allowlisted path, wrong method
    ok = s in (404, 405) and "not proxied" in body
    print(f"[{'OK' if ok else 'FAIL'}] BFF disallowed method DELETE /models -> {s} (expect 404/405)")
    failures += 0 if ok else 1

    # 4. Origin guard: a foreign Origin is rejected (403) before any key injection (CSRF defense).
    s, body, _ = _req(f"{UI}/api/gw/models", headers={"Origin": "http://evil.example"})
    ok = s == 403
    print(f"[{'OK' if ok else 'FAIL'}] BFF foreign Origin -> {s} (expect 403)")
    failures += 0 if ok else 1

    # 5. Security headers on a console route (CSP frame-ancestors none, nosniff, referrer policy).
    s, _, hdrs = _req(f"{UI}/infer")
    h = {k.lower(): v for k, v in hdrs.items()}
    csp = h.get("content-security-policy", "")
    checks = [
        ("CSP present", bool(csp)),
        ("frame-ancestors 'none'", "frame-ancestors 'none'" in csp),
        ("default-src 'self'", "default-src 'self'" in csp),
        ("X-Content-Type-Options nosniff", h.get("x-content-type-options", "").lower() == "nosniff"),
        ("Referrer-Policy set", bool(h.get("referrer-policy"))),
    ]
    for label, good in checks:
        print(f"[{'OK' if good else 'FAIL'}] header: {label}")
        failures += 0 if good else 1

    if failures:
        print(f"\n{failures} security check(s) failed.")
        return 1
    print("\nT077/T085 PASS — key server-side + allowlisted-only + origin-guarded; headers present; "
          "UI bound to 127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
