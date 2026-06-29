#!/usr/bin/env python3
"""Submit ground-truth labels for logged predictions (013 US1, T241, FR-120).

Thin batch client over the gateway's `POST /monitor/labels` — the single label-ingestion code path
(online or bulk). Reads a JSONL file of `{"prediction_id": ..., "label": ...}` rows and loops the
endpoint, so **delayed** labels (minutes to days after serving) attach to their predictions by id. An
unknown id or an already-labeled prediction is reported per-row and never overwrites served history.

Usage:
    python data/submit_labels.py <labels.jsonl> [--gateway URL] [--api-key KEY]

Each line: {"prediction_id": "ab12…", "label": "cat"}   (label may be a string / number / object)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_KEY_HEADER = "X-API-Key"


def resolve_api_key(flag=None):
    """Gateway API key from (in order): --api-key, GATEWAY_API_KEY, first key in
    GATEWAY_API_KEYS_FILE — mirrors register_dataset.py / the test auth-header contract."""
    if flag:
        return flag
    env = os.getenv("GATEWAY_API_KEY")
    if env:
        return env
    path = os.getenv("GATEWAY_API_KEYS_FILE")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                k = line.strip()
                if k and not k.startswith("#"):
                    return k
    return None


def _post_label(gateway, headers, row):
    body = json.dumps({"prediction_id": row["prediction_id"], "label": row["label"]}).encode()
    req = urllib.request.Request(f"{gateway}/monitor/labels", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"prediction_id": row.get("prediction_id"), "status": "error",
                "detail": e.read().decode()[:200]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Submit ground-truth labels for logged predictions.")
    ap.add_argument("path", help="JSONL of {prediction_id, label} rows")
    ap.add_argument("--gateway", default=os.getenv("GATEWAY_URL", "http://localhost:8080"))
    ap.add_argument("--api-key", default=None,
                    help="gateway API key (else GATEWAY_API_KEY / GATEWAY_API_KEYS_FILE)")
    args = ap.parse_args()

    headers = {"Content-Type": "application/json"}
    key = resolve_api_key(args.api_key)
    if key:
        headers[API_KEY_HEADER] = key

    counts = {"attached": 0, "duplicate": 0, "unknown": 0, "error": 0}
    with open(args.path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "prediction_id" not in row or "label" not in row:
                print(f"[SKIP] row missing prediction_id/label: {line[:120]}", file=sys.stderr)
                continue
            res = _post_label(args.gateway, headers, row)
            status = res.get("status", "error")
            counts[status] = counts.get(status, 0) + 1
            print(f"{status:9} {res.get('prediction_id')}")

    print(f"\nattached={counts['attached']} duplicate={counts['duplicate']} "
          f"unknown={counts['unknown']} error={counts['error']}")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
