"""Phase 6 fine-tune integration test (T033, US4) — closes the loop.

register dataset -> launch LoRA run -> poll to completion -> a new registered model version
appears, linked to the run + dataset version, is promotable, and carries a servable GGUF
artifact. Also asserts the run's full config is recorded (reproducibility, FR-012).

Requires: stack up (serve_up.ps1 with TRAINER_URL injected) AND the native trainer running
(`bash training/run.sh`). First run downloads the small base model, so the poll timeout is generous.
Exits non-zero on failure; SKIPs cleanly if the trainer isn't running.
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
DATASET = "qa-demo"
OUTPUT = "qwen0_5b-qa-lora"
POLL_TIMEOUT = int(os.getenv("FINETUNE_TIMEOUT", "1200"))

DEMO_ROWS = [
    {"instruction": "What is MLOps-Lite?", "response": "A lightweight local MLOps platform."},
    {"instruction": "What GPU rule does it enforce?", "response": "One model in VRAM at a time."},
    {"instruction": "How are datasets versioned?", "response": "By content hash on Garage."},
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
    # 0. Trainer up?
    _, th = _req("GET", "/training/health")
    if not th.get("reachable"):
        print("[SKIP] trainer not running (bash training/run.sh) — skipping US4 test")
        return 0
    print(f"[OK] trainer reachable (gpu_free={th.get('gpu_free_mib')} MiB)")

    # 1. Register a tiny instruction dataset (JSONL).
    jsonl = ("\n".join(json.dumps(r) for r in DEMO_ROWS)).encode()
    sd, ds = _req("POST", "/datasets",
                  {"name": DATASET, "content_b64": base64.b64encode(jsonl).decode(), "format": "jsonl"})
    if sd != 201:
        print(f"[FAIL] dataset register -> {sd} {ds}")
        return 1
    version = ds["version"]
    print(f"[OK] dataset {DATASET}@{version} registered ({ds['size_bytes']} bytes)")

    # 2. Launch a short LoRA run on that pinned version.
    sl, run = _req("POST", "/runs",
                   {"dataset_name": DATASET, "dataset_version": version,
                    "output_name": OUTPUT, "steps": 5, "lora_r": 8, "seed": 0})
    if sl not in (200, 202):
        print(f"[FAIL] launch -> {sl} {run}")
        return 1
    run_id = run["run_id"]
    print(f"[OK] launched run {run_id}; polling up to {POLL_TIMEOUT}s ...")

    # 3. Poll to completion.
    deadline = time.time() + POLL_TIMEOUT
    rec = {}
    while time.time() < deadline:
        _, rec = _req("GET", f"/runs/{run_id}")
        st = rec.get("status")
        if st in ("completed", "failed"):
            break
        time.sleep(5)
    if rec.get("status") != "completed":
        print(f"[FAIL] run did not complete: status={rec.get('status')} error={rec.get('error')}")
        return 1
    mv = rec["model"]
    print(f"[OK] run completed: loss={rec['metrics'].get('train_loss'):.4f} "
          f"-> {mv['name']} v{mv['version']} ({rec['metrics'].get('device')})")

    # 4. Reproducibility: the run's full config is recorded (FR-012).
    params = rec.get("params") or {}
    needed = {"base_model", "dataset_name", "dataset_version", "steps", "lora_r", "seed"}
    if not needed <= set(params) or params.get("dataset_version") != version:
        print(f"[FAIL] run config not fully recorded: {params}")
        return 1
    print(f"[OK] run config recorded (reproducible): {params}")

    # 5. New version is lineage-linked, promotable, and carries a servable GGUF artifact.
    sm, model = _req("GET", f"/models/{OUTPUT}")
    ver = next((v for v in model.get("versions", []) if v["version"] == mv["version"]), None)
    tags = (ver or {}).get("tags", {})
    if not ver or tags.get("dataset_version") != version or not (ver.get("source") or "").endswith(".gguf"):
        print(f"[FAIL] registered version lineage/artifact wrong: {ver}")
        return 1
    print(f"[OK] version lineage: base={tags.get('base_model')} dataset={tags.get('dataset_version')} "
          f"source={ver.get('source')}")

    sp, pr = _req("POST", f"/models/{OUTPUT}/promote", {"version": mv["version"]})
    if sp != 200 or pr.get("serving_version") != mv["version"]:
        print(f"[FAIL] promote -> {sp} {pr}")
        return 1
    print(f"[OK] promoted {OUTPUT} v{mv['version']} to serving")

    print("\nT033 PASS — dataset -> tracked LoRA run -> registered, promotable, servable version")
    return 0


def test_finetune(require_trainer):
    """Pytest wrapper (005 US5): skip unless the native trainer daemon is reachable."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
