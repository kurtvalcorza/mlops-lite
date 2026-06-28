"""Phase 4 registry integration test (T023, US2).

Exercises the full registry lifecycle through the gateway: register two versions of a model,
promote the second, and confirm the serving pointer (and US1 /infer) resolve to it. Robust to
re-runs — it captures the actual version numbers MLflow assigns rather than assuming 1/2.

Requires the stack up (serve_up.ps1 / `make up`). The /infer assertion is skipped if the native
serving backend isn't running, so the registry test stands alone. Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
# Use the model name the serving path resolves, so the optional /infer check lines up.
MODEL = os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GW + path, data=data,
        headers=auth_headers({"Content-Type": "application/json"}), method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    # 1. Register two versions (arbitrary sources — registry tracks metadata, not the GGUF bytes).
    s1, v1 = _req("POST", "/models", {"name": MODEL, "source": "s3://models/base/v1", "tags": {"kind": "base"}})
    s2, v2 = _req("POST", "/models", {"name": MODEL, "source": "s3://models/base/v2", "tags": {"kind": "base"}})
    if s1 != 201 or s2 != 201:
        print(f"[FAIL] register -> {s1} {v1} / {s2} {v2}")
        return 1
    va, vb = v1["version"], v2["version"]
    print(f"[OK] registered {MODEL} versions {va} and {vb}")

    # 2. Promote the second version to serving.
    sp, pr = _req("POST", f"/models/{MODEL}/promote", {"version": vb})
    if sp != 200 or pr.get("serving_version") != str(vb):
        print(f"[FAIL] promote -> {sp} {pr}")
        return 1
    print(f"[OK] promoted version {vb} to serving")

    # 3. Serving pointer resolves to the promoted version; both versions are listed/comparable.
    sg, model = _req("GET", f"/models/{MODEL}")
    serving_v = (model.get("serving") or {}).get("version")
    listed = {v["version"] for v in model.get("versions", [])}
    if sg != 200 or serving_v != str(vb) or not {va, vb} <= listed:
        print(f"[FAIL] resolve -> serving={serving_v} listed={listed}")
        return 1
    print(f"[OK] /models/{MODEL} serving={serving_v}, {len(listed)} versions listed")

    # 4. Promotion is repointable — promote back to the first version, then return to the second.
    _req("POST", f"/models/{MODEL}/promote", {"version": va})
    _, m2 = _req("GET", f"/models/{MODEL}")
    if (m2.get("serving") or {}).get("version") != str(va):
        print("[FAIL] re-promote did not move the serving pointer")
        return 1
    _req("POST", f"/models/{MODEL}/promote", {"version": vb})
    print(f"[OK] serving pointer is repointable (moved to {va} and back to {vb})")

    # 5. Optional end-to-end: US1 /infer surfaces the promoted registry version.
    _, sh = _req("GET", "/serving/health")
    if sh.get("reachable"):
        si, inf = _req("POST", "/infer", {"prompt": "Reply with the single word: pong", "max_tokens": 8})
        if si == 200 and inf.get("registry_version") == str(vb):
            print(f"[OK] /infer resolved registry_version={inf['registry_version']} (model served)")
        else:
            print(f"[FAIL] /infer registry_version -> {si} {inf.get('registry_version')}")
            return 1
    else:
        print("[SKIP] /infer registry resolution — native serving backend not running")

    print("\nT023 PASS — register -> promote -> serving resolution verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
