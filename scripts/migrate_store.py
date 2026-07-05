#!/usr/bin/env python3
"""Object-store migration mirror (020 T403, US1) — contracts/store-migration.md.

A boto3 source→dest mirror for the 020 store-exit migration (FR-199/FR-200), deliberately NOT
rclone/`mc` (research R3: zero new tools; the retired store's own mirror tooling was archived upstream):

  * **Idempotent** — an object is copied only if absent at dest or its size differs; a clean
    re-run reports `copied == 0` on every bucket (SC-127).
  * **Bidirectional** — `--reverse` swaps the roles (the rollback re-mirror); same code path.
  * **Streaming** — object bodies stream source→dest via `upload_fileobj` (no full-file
    buffering; the models bucket carries multi-GB artifacts on a constrained drive).
  * **Pagination-safe** — listings page past 1,000 keys (the platformlib.store discipline).
  * **Report** — the exact MigrationReport shape from data-model.md; exit 0 **iff**
    `parity == true`. Parity compares object counts + byte totals, deliberately never ETags
    (multipart ETags are not portable across S3 implementations).

Concurrent-write note (contract §concurrent-write): live writes during migration are expected;
repeat forward runs until the delta is small, then quiesce writers and run the FINAL pass,
which must land `copied == 0` everywhere. Un-quiesced runs never gate anything.

Deletion note (@claude PR#54): this is an additive-only mirror — it never deletes at dest. A
key deleted from SOURCE after it was copied leaves a stale dest copy, and parity then reports
false (dest > source) on every re-run — deliberately non-converging, so a delete racing the
migration can't be papered over. The operator resolves it explicitly (delete the stale dest
key, or accept it pre-quiesce and let the final quiesced pass gate) — relevant to T405/T406.

Usage:
  python scripts/migrate_store.py \
    --source-endpoint URL --source-key K --source-secret S \
    --dest-endpoint URL   --dest-key K   --dest-secret S \
    [--buckets datasets,models,results,mlflow] [--reverse] [--report PATH]
"""
import argparse
import json
import sys
import time

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

DEFAULT_BUCKETS = ("datasets", "models", "results", "mlflow")


def make_client(endpoint: str, access_key: str, secret_key: str, region: str = "us-east-1"):
    """Signature-v4 path-style client — the same posture every platform client already uses."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def list_bucket(client, bucket: str) -> dict:
    """Full {key: size} inventory, paging past the 1,000-key ceiling. Missing bucket fails loud
    (bootstrap owns bucket creation — garage-init — not this tool)."""
    inventory = {}
    token = None
    while True:
        kwargs = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            page = client.list_objects_v2(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchBucket", "404"):
                raise SystemExit(
                    f"bucket '{bucket}' missing on {client.meta.endpoint_url} — run the store "
                    "bootstrap first (garage-init); this tool never creates buckets"
                ) from e
            raise
        for obj in page.get("Contents", []) or []:
            inventory[obj["Key"]] = obj["Size"]
        if not page.get("IsTruncated"):
            return inventory
        token = page.get("NextContinuationToken")
        if not token:  # defensive: truncated page without a token would otherwise loop forever
            return inventory


def mirror_bucket(source, dest, bucket: str) -> dict:
    """One bucket's copy pass + post-pass parity counts (data-model.md `buckets[]` entry)."""
    src_inv = list_bucket(source, bucket)
    dest_inv = list_bucket(dest, bucket)
    copied = skipped = 0
    for key in sorted(src_inv):
        if dest_inv.get(key) == src_inv[key]:
            skipped += 1
            continue
        # ContentType rides on the GET itself (@claude PR#54): no separate HEAD round trip per
        # copied object, and no HEAD→GET TOCTOU window.
        obj = source.get_object(Bucket=bucket, Key=key)
        extra = {"ContentType": obj["ContentType"]} if obj.get("ContentType") else None
        # upload_fileobj streams (multipart past its threshold) — no full-object buffering.
        dest.upload_fileobj(obj["Body"], bucket, key, ExtraArgs=extra)
        copied += 1
    # Parity is counted AFTER the copy pass, on both sides (data-model.md note) — a live writer
    # racing this run shows up as a source/dest delta here, never as silent agreement.
    src_after = list_bucket(source, bucket)
    dest_after = list_bucket(dest, bucket)
    return {
        "name": bucket,
        "source": {"objects": len(src_after), "bytes": sum(src_after.values())},
        "dest": {"objects": len(dest_after), "bytes": sum(dest_after.values())},
        "copied": copied,
        "skipped": skipped,
    }


def run_migration(source, dest, buckets, direction: str) -> dict:
    started = time.time()
    entries = [mirror_bucket(source, dest, b) for b in buckets]
    return {
        "direction": direction,
        "started_at": started,
        "finished_at": time.time(),
        "buckets": entries,
        "parity": all(e["source"] == e["dest"] for e in entries),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-endpoint", required=True)
    ap.add_argument("--source-key", required=True)
    ap.add_argument("--source-secret", required=True)
    ap.add_argument("--dest-endpoint", required=True)
    ap.add_argument("--dest-key", required=True)
    ap.add_argument("--dest-secret", required=True)
    ap.add_argument("--buckets", default=",".join(DEFAULT_BUCKETS),
                    help="comma-separated bucket list (default: all four platform buckets)")
    ap.add_argument("--reverse", action="store_true",
                    help="swap source/dest roles (rollback re-mirror) — same code path")
    ap.add_argument("--report", default=None,
                    help="write the JSON MigrationReport here (default: stdout)")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args(argv)

    source = make_client(args.source_endpoint, args.source_key, args.source_secret, args.region)
    dest = make_client(args.dest_endpoint, args.dest_key, args.dest_secret, args.region)
    if args.reverse:
        source, dest = dest, source

    buckets = [b.strip() for b in args.buckets.split(",") if b.strip()]
    report = run_migration(source, dest, buckets,
                           direction="reverse" if args.reverse else "forward")

    payload = json.dumps(report, indent=2)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)

    for entry in report["buckets"]:
        print(f"[{report['direction']}] {entry['name']}: copied={entry['copied']} "
              f"skipped={entry['skipped']} source={entry['source']} dest={entry['dest']}",
              file=sys.stderr)
    print(f"parity: {report['parity']}", file=sys.stderr)
    return 0 if report["parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
