"""010 US2 — embeddings contrastive fine-tune integration test (T190 — SC-058).

register a tiny pairs dataset -> launch a `modality=embeddings` run -> poll to completion -> a new
registered version appears tagged `task=embedding` / `serving_engine=bentoml` /
`framework=sentence-transformers` + lineage, logs a `cosine_spread` eval metric, and is promotable so
009's embeddings service loads it (`models:/{name}@serving`, the ST-flavor load contract). Promotion
stays manual (010 never auto-promotes).

Requires the native trainer (`bash training/run.sh`); SKIPs cleanly otherwise.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
DATASET = "embed-demo"
OUTPUT = "embed-demo-ft"
POLL_TIMEOUT = int(os.getenv("EMBED_FINETUNE_TIMEOUT", "900"))

# anchor/positive pairs (in-batch negatives → MultipleNegativesRankingLoss, the default).
ROWS = [
    {"anchor": "how do I reset my password", "positive": "steps to recover account access"},
    {"anchor": "what is the refund policy", "positive": "how to get money back on an order"},
    {"anchor": "track my shipment", "positive": "where is my package right now"},
    {"anchor": "cancel my subscription", "positive": "end my recurring membership"},
]


def _req(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GW + path, data=data,
                                 headers=auth_headers({"Content-Type": "application/json"}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> int:
    _, th = _req("GET", "/training/health")
    if not th.get("reachable"):
        print("[SKIP] trainer not running (bash training/run.sh) — skipping US2 embeddings test")
        return 0
    print(f"[OK] trainer reachable (gpu_free={th.get('gpu_free_mib')} MiB)")

    jsonl = ("\n".join(json.dumps(r) for r in ROWS)).encode()
    sd, ds = _req("POST", "/datasets",
                  {"name": DATASET, "content_b64": base64.b64encode(jsonl).decode(), "format": "jsonl"})
    if sd != 201:
        print(f"[FAIL] dataset register -> {sd} {ds}")
        return 1
    version = ds["version"]
    print(f"[OK] dataset {DATASET}@{version} registered ({len(ROWS)} pairs)")

    sl, run = _req("POST", "/runs",
                   {"dataset_name": DATASET, "dataset_version": version, "output_name": OUTPUT,
                    "modality": "embeddings", "epochs": 1, "seed": 0})
    if sl not in (200, 202):
        print(f"[FAIL] launch -> {sl} {run}")
        return 1
    run_id = run["run_id"]
    print(f"[OK] launched embeddings run {run_id}; polling up to {POLL_TIMEOUT}s ...")

    deadline = time.time() + POLL_TIMEOUT
    rec = {}
    while time.time() < deadline:
        _, rec = _req("GET", f"/runs/{run_id}")
        if rec.get("status") in ("completed", "failed"):
            break
        time.sleep(5)
    if rec.get("status") != "completed":
        print(f"[FAIL] run did not complete: status={rec.get('status')} error={rec.get('error')}")
        return 1
    mv = rec["model"]
    metrics = rec.get("metrics") or {}
    if "cosine_spread" not in metrics:
        print(f"[FAIL] missing cosine_spread eval metric: {metrics}")
        return 1
    print(f"[OK] run completed: cosine_spread={metrics['cosine_spread']:.4f} -> {mv['name']} v{mv['version']}")

    sm, model = _req("GET", f"/models/{OUTPUT}")
    ver = next((v for v in model.get("versions", []) if v["version"] == mv["version"]), None)
    tags = (ver or {}).get("tags", {})
    need = {"task": "embedding", "serving_engine": "bentoml",
            "framework": "sentence-transformers", "lineage": "base"}
    if not ver or any(tags.get(k) != v for k, v in need.items()) or tags.get("dataset_version") != version:
        print(f"[FAIL] registered version tags wrong: {tags}")
        return 1
    print(f"[OK] version tags: task={tags['task']} engine={tags['serving_engine']} "
          f"framework={tags['framework']} lineage={tags['lineage']}")

    sp, pr = _req("POST", f"/models/{OUTPUT}/promote", {"version": mv["version"]})
    if sp != 200 or pr.get("serving_version") != mv["version"]:
        print(f"[FAIL] promote -> {sp} {pr}")
        return 1
    print(f"[OK] promoted {OUTPUT} v{mv['version']} to serving")

    print("\nT190 PASS — pairs dataset -> tracked contrastive run -> tagged, promotable embeddings version")
    return 0


def test_embeddings_finetune(require_trainer):
    """Pytest wrapper: skip unless the native trainer daemon is reachable."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
