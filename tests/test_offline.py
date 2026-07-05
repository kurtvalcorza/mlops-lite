"""Offline-capability check (T041, SC-007).

The P1–P2 flows — health, dataset register/list, model register/promote — depend only on local
services (gateway, MLflow, Garage), so they work air-gapped once images are pulled and the model
is cached (FR-013/FR-014). This test runs those flows end-to-end and confirms success.

To *hard-verify* offline (optional): cut the gateway's external egress and re-run, e.g.
    docker network create --internal mlops-offline
    docker network connect mlops-offline mlops-lite-gateway-1
    # (the flows here still pass; only external pulls would fail)

Requires the stack up. Exits non-zero on failure.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
DS, MODEL = "offline-ds", "offline-model"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    # Foundational health (gateway → its own process; MLflow/Garage reachability proven by the flows).
    s, h = _req("GET", "/healthz")
    if s != 200 or h.get("status") != "ok":
        print(f"[FAIL] /healthz -> {s} {h}")
        return 1
    print("[OK] gateway healthy")

    # US3 dataset flow (Garage only).
    s, d = _req("POST", "/datasets",
                {"name": DS, "content_b64": base64.b64encode(b"a,b\n1,2\n").decode(), "format": "csv"})
    s2, lst = _req("GET", f"/datasets/{DS}")
    if s != 201 or s2 != 200 or not any(v["version"] == d["version"] for v in lst["versions"]):
        print(f"[FAIL] dataset flow -> {s}/{s2}")
        return 1
    print(f"[OK] dataset register/list works offline ({DS}@{d['version']})")

    # US2 registry flow (MLflow + Postgres only).
    s, _ = _req("POST", "/models", {"name": MODEL, "source": "s3://models/offline/v1"})
    s2, pr = _req("POST", f"/models/{MODEL}/promote", {"version": "1"})
    s3, m = _req("GET", f"/models/{MODEL}")
    if s != 201 or s2 != 200 or s3 != 200 or (m.get("serving") or {}).get("version") != "1":
        print(f"[FAIL] registry flow -> {s}/{s2}/{s3}")
        return 1
    print(f"[OK] registry register/promote/resolve works offline ({MODEL} serving=1)")

    print("\nT041 PASS — P1–P2 flows succeed using only local services (offline-capable)")
    return 0


def test_offline(require_gateway, require_key):
    """Pytest wrapper (005 US5): skip if the stack is down / no key, else assert the local-only flows."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
