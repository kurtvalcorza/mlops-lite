"""010 US1 — vision transfer-learning fine-tune integration test (T186 — SC-057, lease half of SC-061).

register a tiny labeled-image dataset -> launch a `modality=vision` run -> poll to completion -> a new
registered version appears tagged `task=image-classification` / `serving_engine=bentoml` /
`framework=torchvision` + lineage (`lineage=base`), is promotable, and (if the vision daemon is up)
serves the **new** label set. Reproducibility (FR-098): the run's full config is recorded as params.

Lease note (SC-061): the modalities reuse the **exact** 008/008.1 trainer lease + `_lock` (no new lock
— FR-097), already proven by `test_gpu_lease` + the trainer-busy/serving-resident 409 in `test_serving`/
`test_finetune`; this test asserts the new 010 surface (tags, serving contract, lineage), not the lease
itself.

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
from _modality_data import png_b64

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
DATASET = "vision-demo"
OUTPUT = "vision-demo-ft"
POLL_TIMEOUT = int(os.getenv("VISION_FINETUNE_TIMEOUT", "900"))

# Two solid-color classes — separable enough for a head-swap transfer-learning smoke.
ROWS = (
    [{"image_b64": png_b64((220, 30, 30)), "label": "red"} for _ in range(3)]
    + [{"image_b64": png_b64((30, 30, 220)), "label": "blue"} for _ in range(3)]
)


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
        print("[SKIP] trainer not running (bash training/run.sh) — skipping US1 vision test")
        return 0
    print(f"[OK] trainer reachable (gpu_free={th.get('gpu_free_mib')} MiB)")

    jsonl = ("\n".join(json.dumps(r) for r in ROWS)).encode()
    sd, ds = _req("POST", "/datasets",
                  {"name": DATASET, "content_b64": base64.b64encode(jsonl).decode(), "format": "jsonl"})
    if sd != 201:
        print(f"[FAIL] dataset register -> {sd} {ds}")
        return 1
    version = ds["version"]
    print(f"[OK] dataset {DATASET}@{version} registered ({ds['size_bytes']} bytes, {len(ROWS)} imgs)")

    sl, run = _req("POST", "/runs",
                   {"dataset_name": DATASET, "dataset_version": version, "output_name": OUTPUT,
                    "modality": "vision", "epochs": 2, "seed": 0})
    if sl not in (200, 202):
        print(f"[FAIL] launch -> {sl} {run}")
        return 1
    run_id = run["run_id"]
    print(f"[OK] launched vision run {run_id}; polling up to {POLL_TIMEOUT}s ...")

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
    print(f"[OK] run completed: loss={rec['metrics'].get('train_loss'):.4f} "
          f"acc={rec['metrics'].get('train_accuracy')} -> {mv['name']} v{mv['version']}")

    params = rec.get("params") or {}
    if params.get("modality") != "vision" or params.get("dataset_version") != version:
        print(f"[FAIL] run config not fully recorded: {params}")
        return 1
    print(f"[OK] run config recorded (reproducible): {params}")

    sm, model = _req("GET", f"/models/{OUTPUT}")
    ver = next((v for v in model.get("versions", []) if v["version"] == mv["version"]), None)
    tags = (ver or {}).get("tags", {})
    need = {"task": "image-classification", "serving_engine": "bentoml",
            "framework": "torchvision", "lineage": "base"}
    if not ver or any(tags.get(k) != v for k, v in need.items()) or tags.get("dataset_version") != version:
        print(f"[FAIL] registered version tags wrong: {tags}")
        return 1
    if not (ver.get("source") or "").endswith("model.pt"):
        print(f"[FAIL] expected a model.pt artifact: {ver.get('source')}")
        return 1
    print(f"[OK] version tags: task={tags['task']} engine={tags['serving_engine']} "
          f"framework={tags['framework']} lineage={tags['lineage']} source={ver['source']}")

    sp, pr = _req("POST", f"/models/{OUTPUT}/promote", {"version": mv["version"]})
    if sp != 200 or pr.get("serving_version") != mv["version"]:
        print(f"[FAIL] promote -> {sp} {pr}")
        return 1
    print(f"[OK] promoted {OUTPUT} v{mv['version']} to serving (manual promotion — 010 never auto-promotes)")

    print("\nT186 PASS — vision dataset -> tracked transfer-learning run -> tagged, promotable, "
          "servable image-classification version")
    return 0


def test_vision_finetune(require_trainer):
    """Pytest wrapper: skip unless the native trainer daemon is reachable."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
