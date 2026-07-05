"""009 US2 embeddings integration test (T164 → SC-052).

Embeds a batch of texts through the gateway → BentoML embeddings service (CPU, off-lease). Asserts:
  - a list of equal-dimension float vectors, one per input text,
  - the call **succeeds while a GPU tenant holds the lease** (embeddings is off-lease / CPU-only,
    always-available — parity with tabular): /embed never reads /serving/state and is independent of
    the lease holder, so this prints the current holder and still requires a 200,
  - batching: a multi-text request returns one vector per text (the `batchable=True` path).

SKIPs cleanly if the embeddings daemon isn't running. Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"


def _req(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    _, h = _req("GET", "/embed/health")
    if not h.get("reachable"):
        print("[SKIP] embeddings service not running (serving/children/embed_run.sh)")
        return 0
    print(f"[OK] embeddings service reachable ({h.get('backend')})")

    # Note the current GPU lease holder — /embed must succeed regardless (off-lease, always-available).
    _, st = _req("GET", "/serving/state")
    holder = st.get("holder")
    print(f"[i] GPU lease holder right now: {holder!r} — /embed must succeed regardless (off-lease)")

    texts = ["the quick brown fox", "a slow green turtle", "machine learning operations"]
    s, res = _req("POST", "/embed", {"texts": texts})
    if s != 200:
        print(f"[FAIL] embed -> {s} {res}")
        return 1
    vectors = res.get("vectors")
    ok = (isinstance(vectors, list) and len(vectors) == len(texts)
          and all(isinstance(v, list) and len(v) == len(vectors[0]) and len(v) > 0 for v in vectors)
          and all(isinstance(x, (int, float)) for x in vectors[0]))
    if not ok:
        print(f"[FAIL] malformed embeddings (expected {len(texts)} equal-dim vectors): "
              f"count={res.get('count')} dim={res.get('dim')}")
        return 1
    print(f"[OK] embedded {len(texts)} texts -> {res.get('count')} vectors of dim {res.get('dim')} "
          f"on {res.get('device')} (off-lease; holder was {holder!r})")

    # Single-text request also returns one vector of the same dimension (batchable edge: batch of 1).
    s1, one = _req("POST", "/embed", {"texts": ["just one"]})
    if s1 != 200 or one.get("count") != 1 or one.get("dim") != res.get("dim"):
        print(f"[FAIL] single-text embed -> {s1} count={one.get('count')} dim={one.get('dim')}")
        return 1
    print(f"[OK] single-text embed -> 1 vector of dim {one.get('dim')} (batchable path)")

    print("\nT164 PASS — embeddings: equal-dim vectors, CPU/off-lease always-available, batchable")
    return 0


def test_embed(require_embed, require_key):
    """Pytest wrapper (005 US5): skip unless the embeddings daemon is reachable + a key is set."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
