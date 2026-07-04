"""009 US4 tabular integration test (T174 → SC-054, SC-055).

Predicts a batch of rows through the gateway → BentoML tabular service (LightGBM, CPU, off-lease).
Asserts:
  - one prediction per input row from the single joblib artifact (FR-080),
  - the call **succeeds while a GPU tenant holds the lease** (tabular is off-lease / always-available
    — parity with embeddings): /predict never reads /serving/state, so this prints the current holder
    and still requires a 200,
  - missing features default server-side (a sparse row still predicts).

SKIPs cleanly if the tabular daemon isn't running. Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"


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
    _, h = _req("GET", "/predict/health")
    if not h.get("reachable"):
        print("[SKIP] tabular service not running (serving/children/tabular_run.sh)")
        return 0
    print(f"[OK] tabular service reachable ({h.get('backend')})")

    # Note the GPU lease holder — /predict must succeed regardless (off-lease, always-available).
    _, st = _req("GET", "/serving/state")
    holder = st.get("holder")
    print(f"[i] GPU lease holder right now: {holder!r} — /predict must succeed regardless (off-lease)")

    rows = [
        {"f0": 1.2, "f1": -0.4, "f2": 0.3, "f3": 0.0},
        {"f0": -1.5, "f1": 2.0},  # sparse — missing features default server-side
        {"f0": 0.0, "f1": 0.0, "f2": 0.0, "f3": 0.0},
    ]
    s, res = _req("POST", "/predict", {"rows": rows})
    if s != 200:
        print(f"[FAIL] predict -> {s} {res}")
        return 1
    preds = res.get("predictions")
    ok = (isinstance(preds, list) and len(preds) == len(rows)
          and all("prediction" in p and "score" in p for p in preds)
          and all(p["prediction"] in (0, 1) for p in preds))
    if not ok:
        print(f"[FAIL] malformed predictions (expected {len(rows)} rows): {res}")
        return 1
    print(f"[OK] predicted {len(preds)} rows -> labels={[p['prediction'] for p in preds]} "
          f"on {res.get('device')} (off-lease; holder was {holder!r})")

    print("\nT174 PASS — tabular: one prediction per row, CPU/off-lease always-available")
    return 0


def test_tabular(require_tabular, require_key):
    """Pytest wrapper (005 US5): skip unless the tabular daemon is reachable + a key is set."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
