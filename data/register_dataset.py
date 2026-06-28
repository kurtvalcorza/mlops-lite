#!/usr/bin/env python3
"""Register a local file as an immutable dataset version (T025, US3).

Thin client over the gateway's POST /datasets so there's a single registration code path. The
dataset version is the sha256 of the file's bytes, so re-registering an unchanged file is a
no-op and any edit produces a new pinned version.

Usage:
    python data/register_dataset.py <name> <path/to/file> [--format csv] [--gateway URL]
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(description="Register a file as an immutable dataset version.")
    ap.add_argument("name", help="dataset name (versions accumulate under it)")
    ap.add_argument("path", help="path to the local file to register")
    ap.add_argument("--format", default=None, help="optional format hint, e.g. csv/jsonl/parquet")
    ap.add_argument("--gateway", default=os.getenv("GATEWAY_URL", "http://localhost:8080"))
    args = ap.parse_args()

    with open(args.path, "rb") as f:
        content = f.read()
    body = json.dumps({
        "name": args.name,
        "content_b64": base64.b64encode(content).decode(),
        "format": args.format,
    }).encode()

    req = urllib.request.Request(
        f"{args.gateway}/datasets", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            manifest = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"[ERROR] {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return 1

    state = "already registered" if manifest.get("already_existed") else "registered"
    print(f"{state}: {manifest['name']} @ {manifest['version']} "
          f"({manifest['size_bytes']} bytes)\n  uri: {manifest['uri']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
