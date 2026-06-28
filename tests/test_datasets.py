"""Phase 5 dataset registry integration test (T027, US3).

Registers a dataset, changes it, re-registers — and confirms both versions are distinct and
independently resolvable, plus that identical content is idempotent (no duplicate version).
Requires the stack up (`make up` / serve_up.ps1). Exits non-zero on failure.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
NAME = "iris-demo"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GW + path, data=data,
        headers=auth_headers({"Content-Type": "application/json"}), method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def _register(content: bytes, fmt="csv"):
    return _req("POST", "/datasets", {
        "name": NAME, "content_b64": base64.b64encode(content).decode(), "format": fmt,
    })


def main() -> int:
    content_a = b"sepal,petal,species\n5.1,1.4,setosa\n"
    content_b = b"sepal,petal,species\n6.2,4.5,versicolor\n"  # changed content

    # 1. Register, then change and re-register.
    sa, va = _register(content_a)
    sb, vb = _register(content_b)
    if sa != 201 or sb != 201:
        print(f"[FAIL] register -> {sa} {va} / {sb} {vb}")
        return 1
    if va["version"] == vb["version"]:
        print(f"[FAIL] changed content produced the same version {va['version']}")
        return 1
    print(f"[OK] registered two versions: {va['version']} and {vb['version']} (distinct)")

    # 2. Idempotency: re-registering content A returns the same version, flagged already_existed.
    sc, vc = _register(content_a)
    if vc["version"] != va["version"] or not vc.get("already_existed"):
        print(f"[FAIL] idempotency -> version={vc['version']} already_existed={vc.get('already_existed')}")
        return 1
    print(f"[OK] re-registering identical content is idempotent (still {vc['version']})")

    # 3. Both versions listed under the dataset name.
    sl, listing = _req("GET", f"/datasets/{NAME}")
    listed = {v["version"] for v in listing.get("versions", [])}
    if sl != 200 or not {va["version"], vb["version"]} <= listed:
        print(f"[FAIL] listing -> {sl} {listed}")
        return 1
    print(f"[OK] /datasets/{NAME} lists {len(listed)} versions")

    # 4. Each version independently resolvable with the correct, distinct sha256 + a download URL.
    s1, m1 = _req("GET", f"/datasets/{NAME}/{va['version']}")
    s2, m2 = _req("GET", f"/datasets/{NAME}/{vb['version']}")
    ok = (
        s1 == 200 and s2 == 200
        and m1["sha256"] != m2["sha256"]
        and m1["size_bytes"] == len(content_a)
        and m2["size_bytes"] == len(content_b)
        and m1.get("download_url") and m2.get("download_url")
    )
    if not ok:
        print(f"[FAIL] resolve -> {s1}/{s2} sha differ={m1.get('sha256') != m2.get('sha256')}")
        return 1
    print("[OK] both versions resolve with distinct sha256, correct sizes, and download URLs")

    # 5. Missing version → 404.
    s404, _ = _req("GET", f"/datasets/{NAME}/deadbeef0000")
    if s404 != 404:
        print(f"[FAIL] expected 404 for missing version, got {s404}")
        return 1
    print("[OK] missing version returns 404")

    print("\nT027 PASS — two registrations -> two distinct retrievable versions")
    return 0


def test_datasets(require_gateway, require_key):
    """Pytest wrapper (005 US5): skip if the stack is down / no key, else assert the dataset flow."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
