"""010 US4 — lineage & adapter-chaining integration test (T198 — SC-060).

Exercises the shared `lineage.py` across a real genealogy, on the **vision** modality (its registered
artifact — `model.pt` — reloads as a trainable warm start, so it is resumable; FR-096):

  1. a **base** vision fine-tune registers v1 with `lineage=base` (no parent),
  2. a fine-tune **resuming from v1** (`parent_version=1`) registers v2 with `lineage=chained`,
     `parent_version=1`, and a `parent_run_id` link to v1's run,
  3. a **wrong-modality** chain is rejected: an `embeddings` run that names the same model and
     `parent_version=1` (a vision/image-classification version) fails before training with a
     cross-modality error — no version registered (FR-096).

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
DATASET = "lineage-demo"
OUTPUT = "lineage-demo-ft"
POLL_TIMEOUT = int(os.getenv("LINEAGE_FINETUNE_TIMEOUT", "900"))

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


def _launch_and_wait(body, timeout=POLL_TIMEOUT):
    sl, run = _req("POST", "/runs", body)
    if sl not in (200, 202):
        return {"status": "rejected", "code": sl, "body": run}
    run_id = run["run_id"]
    deadline = time.time() + timeout
    rec = {}
    while time.time() < deadline:
        _, rec = _req("GET", f"/runs/{run_id}")
        if rec.get("status") in ("completed", "failed"):
            break
        time.sleep(5)
    return rec


def _version_tags(version):
    _, model = _req("GET", f"/models/{OUTPUT}")
    ver = next((v for v in model.get("versions", []) if v["version"] == version), None)
    return (ver or {}).get("tags", {})


def main() -> int:
    _, th = _req("GET", "/training/health")
    if not th.get("reachable"):
        print("[SKIP] trainer not running (bash training/run.sh) — skipping US4 lineage test")
        return 0
    print(f"[OK] trainer reachable (gpu_free={th.get('gpu_free_mib')} MiB)")

    jsonl = ("\n".join(json.dumps(r) for r in ROWS)).encode()
    sd, ds = _req("POST", "/datasets",
                  {"name": DATASET, "content_b64": base64.b64encode(jsonl).decode(), "format": "jsonl"})
    if sd != 201:
        print(f"[FAIL] dataset register -> {sd} {ds}")
        return 1
    version = ds["version"]
    print(f"[OK] dataset {DATASET}@{version} registered")

    # 1. Base run.
    base = _launch_and_wait({"dataset_name": DATASET, "dataset_version": version,
                             "output_name": OUTPUT, "modality": "vision", "epochs": 2, "seed": 0})
    if base.get("status") != "completed":
        print(f"[FAIL] base run did not complete: {base.get('status')} {base.get('error')}")
        return 1
    v1 = base["model"]["version"]
    t1 = _version_tags(v1)
    if t1.get("lineage") != "base" or "parent_version" in t1:
        print(f"[FAIL] base version should be lineage=base, no parent_version: {t1}")
        return 1
    print(f"[OK] base run -> v{v1} (lineage=base)")

    # 2. Chained run resuming from v1.
    chained = _launch_and_wait({"dataset_name": DATASET, "dataset_version": version,
                                "output_name": OUTPUT, "modality": "vision", "epochs": 2,
                                "seed": 1, "parent_version": v1})
    if chained.get("status") != "completed":
        print(f"[FAIL] chained run did not complete: {chained.get('status')} {chained.get('error')}")
        return 1
    v2 = chained["model"]["version"]
    t2 = _version_tags(v2)
    if t2.get("lineage") != "chained" or t2.get("parent_version") != str(v1) or not t2.get("parent_run_id"):
        print(f"[FAIL] chained version tags wrong (want lineage=chained, parent_version={v1}, "
              f"parent_run_id set): {t2}")
        return 1
    print(f"[OK] chained run -> v{v2} (lineage=chained, parent_version={v1}, "
          f"parent_run_id={t2['parent_run_id'][:8]}…)")

    # 3. Wrong-modality chain rejected: an embeddings run naming the same model + the vision v1 parent.
    bad = _launch_and_wait({"dataset_name": DATASET, "dataset_version": version,
                            "output_name": OUTPUT, "modality": "embeddings", "epochs": 1,
                            "parent_version": v1})
    err = (bad.get("error") or "") + json.dumps(bad.get("body") or {})
    if bad.get("status") not in ("failed", "rejected") or "cross-modality" not in err.lower():
        print(f"[FAIL] wrong-modality chain should be rejected with a cross-modality error: {bad}")
        return 1
    print(f"[OK] wrong-modality chain rejected: {err[:90]}…")

    print("\nT198 PASS — base -> chained genealogy recorded; cross-modality chaining rejected")
    return 0


def test_lineage_chaining(require_trainer):
    """Pytest wrapper: skip unless the native trainer daemon is reachable."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
