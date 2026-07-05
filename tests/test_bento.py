"""BentoML vision service integration test (T022, US1 — vision half of FR-002).

Classifies an image through the gateway → BentoML service (model packaged from the MinIO models
bucket). Asserts a well-formed top-5 result. SKIPs cleanly if the bento isn't running.

Requires: stack up + the agent's vision child (serving/children/run.sh — spawned on demand;
seed first via scripts/seed_vision_model.py).
Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
# A minimal valid 1x1 PNG (preprocess upsamples to 224x224); keeps the test stdlib-only + offline.
TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    _, h = _req("GET", "/vision/health")
    if not h.get("reachable"):
        print("[SKIP] BentoML vision service not running (bash serving/children/run.sh)")
        return 0
    print(f"[OK] vision service reachable ({h.get('backend')})")

    s, res = _req("POST", "/vision/classify", {"image_b64": TINY_PNG_B64})
    if s != 200:
        print(f"[FAIL] classify -> {s} {res}")
        return 1
    preds = res.get("predictions")
    device = res.get("device")
    # 008 US2: vision now runs on the GPU under the lease (device=cuda) when a GPU is present; on a
    # CPU-only host it falls back to CPU (lease-exempt). Top-5 shape is identical either way (SC-045).
    ok = (isinstance(preds, list) and len(preds) == 5
          and all("label" in p and "score" in p for p in preds)
          and device in ("cuda", "cpu"))
    if not ok:
        print(f"[FAIL] malformed response: {res}")
        return 1
    print(f"[OK] classify -> top1='{preds[0]['label']}' ({preds[0]['score']}), "
          f"5 preds, model={res.get('model')} on {device}"
          + ("  (GPU lease tenant — 008 US2)" if device == "cuda" else "  (CPU fallback)"))

    print("\nT022/T142 PASS — image classified via BentoML; vision-on-GPU under the lease when present")
    return 0


def test_bento(require_vision):
    """Pytest wrapper (005 US5): skip unless the BentoML vision daemon is reachable."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
