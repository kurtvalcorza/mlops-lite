#!/usr/bin/env python3
"""Run a drift check from the CLI (T034, US5) — thin client over the gateway /monitor/check.

Compares a reference vs a current dataset version (both registered via /datasets) and prints the
PSI drift report. With --retrain, a breach launches a fine-tune run, closing the loop.

Usage:
    python monitoring/drift.py --ref baseline@<ver> --cur live@<ver> \
        [--threshold 0.25] [--retrain qa-demo@<ver>:my-lora]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_KEY_HEADER = "X-API-Key"


def resolve_api_key(flag: str | None = None) -> str | None:
    """Gateway API key from (in order): --api-key, GATEWAY_API_KEY, first key in
    GATEWAY_API_KEYS_FILE. Returns None when none is set (open-mode, unchanged). Mirrors the
    test/auth header contract so the CLIs talk to a hardened gateway (005 US3, FR-043)."""
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


def _split(ref: str):
    name, _, version = ref.partition("@")
    if not version:
        raise SystemExit(f"expected name@version, got {ref!r}")
    return {"name": name, "version": version}


def main() -> int:
    ap = argparse.ArgumentParser(description="PSI drift check via the gateway.")
    ap.add_argument("--ref", required=True, help="reference dataset as name@version")
    ap.add_argument("--cur", required=True, help="current dataset as name@version")
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--retrain", default=None,
                    help="on breach, launch training: dataset@version:output_name")
    ap.add_argument("--gateway", default=os.getenv("GATEWAY_URL", "http://localhost:8080"))
    ap.add_argument("--api-key", default=None,
                    help="gateway API key (else GATEWAY_API_KEY / GATEWAY_API_KEYS_FILE)")
    args = ap.parse_args()

    payload = {"reference": _split(args.ref), "current": _split(args.cur),
               "threshold": args.threshold}
    if args.retrain:
        ds, _, out = args.retrain.partition(":")
        rd = _split(ds)
        payload["retrain"] = {"dataset_name": rd["name"], "dataset_version": rd["version"],
                              "output_name": out or "retrained-lora"}

    headers = {"Content-Type": "application/json"}
    key = resolve_api_key(args.api_key)
    if key:
        headers[API_KEY_HEADER] = key

    req = urllib.request.Request(
        f"{args.gateway}/monitor/check", data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"[ERROR] {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return 1

    rep = res["report"]
    print(f"drift: {rep['current']} vs {rep['reference']}")
    print(f"  max PSI = {rep['max_psi']} (threshold {rep['threshold']}) "
          f"-> {'DRIFT' if rep['dataset_drift'] else 'stable'}")
    for col, p in rep["features"].items():
        print(f"    {col}: {p}{'  <-- drifted' if col in rep['drifted_columns'] else ''}")
    if res.get("retrain"):
        print(f"  retrain: {json.dumps(res['retrain'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
