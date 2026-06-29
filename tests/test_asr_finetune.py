"""010 US3 — ASR (Whisper) fine-tune + HF→ggml integration test (T194 — SC-059, SC-061).

register a tiny audio+transcript dataset -> launch a `modality=asr` run -> poll to completion -> the
flow fine-tunes Whisper-small + LoRA, **merges the adapter + converts** HF→ggml (q8_0), and a new
registered version appears tagged `task=asr` / `serving_engine=whisper.cpp` / `format=ggml` + lineage,
carrying the `ggml-*.bin` artifact; it is promotable so 009's whisper.cpp server can transcribe with it.

This is the heaviest modality and needs the whisper.cpp HF→ggml toolchain (`convert-h5-to-ggml` +
`quantize`) on the trainer host. If that toolchain is absent the converter fails the run cleanly (no
partial version — FR-093), which this test reports as a failure with the captured error. SKIPs when the
trainer daemon is down.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from _auth import auth_headers
from _modality_data import wav_b64

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
DATASET = "asr-demo"
OUTPUT = "asr-demo-ft"
POLL_TIMEOUT = int(os.getenv("ASR_FINETUNE_TIMEOUT", "1800"))

# Distinct tones with short transcripts — a tiny smoke (not an accuracy target; 013 owns eval).
ROWS = [
    {"audio_b64": wav_b64(0.4, 180), "text": "hello world"},
    {"audio_b64": wav_b64(0.4, 320), "text": "open the pod bay doors"},
    {"audio_b64": wav_b64(0.4, 240), "text": "the quick brown fox"},
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
        print("[SKIP] trainer not running (bash training/run.sh) — skipping US3 ASR test")
        return 0
    print(f"[OK] trainer reachable (gpu_free={th.get('gpu_free_mib')} MiB)")

    jsonl = ("\n".join(json.dumps(r) for r in ROWS)).encode()
    sd, ds = _req("POST", "/datasets",
                  {"name": DATASET, "content_b64": base64.b64encode(jsonl).decode(), "format": "jsonl"})
    if sd != 201:
        print(f"[FAIL] dataset register -> {sd} {ds}")
        return 1
    version = ds["version"]
    print(f"[OK] dataset {DATASET}@{version} registered ({len(ROWS)} clips)")

    sl, run = _req("POST", "/runs",
                   {"dataset_name": DATASET, "dataset_version": version, "output_name": OUTPUT,
                    "modality": "asr", "epochs": 1, "lora_r": 8, "seed": 0})
    if sl not in (200, 202):
        print(f"[FAIL] launch -> {sl} {run}")
        return 1
    run_id = run["run_id"]
    print(f"[OK] launched ASR run {run_id}; polling up to {POLL_TIMEOUT}s ...")

    deadline = time.time() + POLL_TIMEOUT
    rec = {}
    while time.time() < deadline:
        _, rec = _req("GET", f"/runs/{run_id}")
        if rec.get("status") in ("completed", "failed"):
            break
        time.sleep(10)
    if rec.get("status") != "completed":
        print(f"[FAIL] run did not complete: status={rec.get('status')} error={rec.get('error')}")
        return 1
    mv = rec["model"]
    metrics = rec.get("metrics") or {}
    if "wer" not in metrics:
        print(f"[FAIL] missing WER-style eval metric: {metrics}")
        return 1
    print(f"[OK] run completed: loss={metrics.get('train_loss')} wer={metrics['wer']} "
          f"-> {mv['name']} v{mv['version']}")

    sm, model = _req("GET", f"/models/{OUTPUT}")
    ver = next((v for v in model.get("versions", []) if v["version"] == mv["version"]), None)
    tags = (ver or {}).get("tags", {})
    need = {"task": "asr", "serving_engine": "whisper.cpp", "format": "ggml", "lineage": "base"}
    if not ver or any(tags.get(k) != v for k, v in need.items()):
        print(f"[FAIL] registered version tags wrong: {tags}")
        return 1
    if ".bin" not in (ver.get("source") or ""):
        print(f"[FAIL] expected a ggml .bin artifact: {ver.get('source')}")
        return 1
    print(f"[OK] version tags: task={tags['task']} engine={tags['serving_engine']} "
          f"format={tags['format']} lineage={tags['lineage']} source={ver['source']}")

    sp, pr = _req("POST", f"/models/{OUTPUT}/promote", {"version": mv["version"]})
    if sp != 200 or pr.get("serving_version") != mv["version"]:
        print(f"[FAIL] promote -> {sp} {pr}")
        return 1
    print(f"[OK] promoted {OUTPUT} v{mv['version']} to serving")

    print("\nT194 PASS — audio dataset -> Whisper+LoRA run -> merged + HF→ggml(q8_0) -> tagged, "
          "promotable asr version")
    return 0


def test_asr_finetune(require_trainer):
    """Pytest wrapper: skip unless the native trainer daemon is reachable."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
