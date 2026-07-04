#!/usr/bin/env python3
"""Garage one-shot bootstrap (020 T401) - the `createbuckets` replacement.

Research R2 planned this as "the garage CLI inside a one-shot container", but the pinned
dxflrs/garage image is scratch-based - no shell, nothing but /garage - so a compose one-shot
cannot script the CLI there. This bootstrap drives Garage's token-authed Admin API (v2)
instead, from a pinned python:3.12-alpine one-shot that shares the garage service's network
namespace (so the unpublished admin port is 127.0.0.1:3903). The contract is unchanged
(contracts/store-migration.md section "bootstrap"): idempotent layout assign/apply -> four
buckets -> one key -> per-bucket read+write grants, and the store-minted S3 key pair is
EMITTED on stdout for `scripts/gen_secrets --record-garage` to record into .env
(credential direction REVERSED vs MinIO: the store mints the key, the operator records it).

Re-running on an initialized node changes nothing and exits 0.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ADMIN = os.environ.get("GARAGE_ADMIN_URL", "http://127.0.0.1:3903")
BUCKETS = ("datasets", "models", "results", "mlflow")
KEY_NAME = os.environ.get("GARAGE_KEY_NAME", "mlops-platform")
# Layout capacity is an accounting hint for Garage's placement math, not a quota; sized to the
# constrained-drive budget by default and env-tunable.
CAPACITY_BYTES = int(os.environ.get("GARAGE_LAYOUT_CAPACITY_BYTES", str(100 * 10**9)))


def _fail(msg: str) -> None:
    print(f"garage-init: {msg}", file=sys.stderr)
    raise SystemExit(1)


TOKEN = os.environ.get("GARAGE_ADMIN_TOKEN") or _fail(
    "GARAGE_ADMIN_TOKEN is required (scripts/gen_secrets --ensure-garage-secrets)"
)


def call(method: str, path: str, body=None, ok=(200,)):
    """One Admin API call; returns (status, parsed-json-or-None). Non-`ok` statuses fail loud."""
    req = urllib.request.Request(
        ADMIN + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        if e.code in ok:
            return e.code, None
        _fail(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        _fail(f"{method} {path} -> {e.reason}")


def wait_for_admin(timeout_s: int = 120) -> None:
    # /health on the admin port is unauthenticated by design (Garage admin API). It answers
    # 503 until a layout is assigned — which is exactly what this one-shot is about to do —
    # so ANY HTTP response (200 or 503) means the API is up; only connection failures wait.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(ADMIN + "/health", timeout=3):
                return
        except urllib.error.HTTPError:
            return  # the API answered (e.g. 503 pre-layout) — it is reachable
        except OSError:
            pass
        print("garage-init: waiting for the garage admin API...", flush=True)
        time.sleep(2)
    _fail(f"admin API not reachable after {timeout_s}s")


def ensure_layout() -> None:
    _, cluster = call("GET", "/v2/GetClusterStatus")
    nodes = cluster["nodes"]
    if not nodes:
        _fail("no nodes visible in GetClusterStatus")
    node = nodes[0]
    if node.get("role"):
        print(f"garage-init: layout already assigned (node {node['id'][:16]}...)")
        return
    print(f"garage-init: assigning layout to node {node['id'][:16]}... "
          f"(zone=local, capacity={CAPACITY_BYTES} bytes)")
    call("POST", "/v2/UpdateClusterLayout", {
        "roles": [{"id": node["id"], "zone": "local",
                   "capacity": CAPACITY_BYTES, "tags": ["mlops-lite"]}],
    })
    call("POST", "/v2/ApplyClusterLayout", {"version": cluster["layoutVersion"] + 1})
    print("garage-init: layout applied")


def ensure_buckets() -> dict:
    """Create any missing platform bucket; return {alias: bucket_id}."""
    def alias_map():
        _, buckets = call("GET", "/v2/ListBuckets")
        return {a: b["id"] for b in buckets for a in b.get("globalAliases", [])}

    have = alias_map()
    for name in BUCKETS:
        if name in have:
            print(f"garage-init: bucket '{name}' exists")
        else:
            call("POST", "/v2/CreateBucket", {"globalAlias": name})
            print(f"garage-init: bucket '{name}' created")
    have = alias_map()
    missing = [b for b in BUCKETS if b not in have]
    if missing:
        _fail(f"buckets missing after create: {missing}")
    return {name: have[name] for name in BUCKETS}


def ensure_key() -> tuple:
    status, info = call(
        "GET", f"/v2/GetKeyInfo?search={KEY_NAME}&showSecretKey=true", ok=(200, 400, 404, 500)
    )
    if status == 200 and info:
        print(f"garage-init: key '{KEY_NAME}' exists ({info['accessKeyId']})")
    else:
        _, info = call("POST", "/v2/CreateKey", {"name": KEY_NAME, "neverExpires": True})
        print(f"garage-init: key '{KEY_NAME}' created ({info['accessKeyId']})")
    secret = info.get("secretAccessKey")
    if not secret:
        _fail(f"key '{KEY_NAME}' returned no secret (showSecretKey should expose it)")
    return info["accessKeyId"], secret


def ensure_grants(bucket_ids: dict, access_key_id: str) -> None:
    for name, bucket_id in bucket_ids.items():
        call("POST", "/v2/AllowBucketKey", {
            "bucketId": bucket_id,
            "accessKeyId": access_key_id,
            "permissions": {"read": True, "write": True, "owner": False},
        })
        print(f"garage-init: read+write granted on '{name}'")


def main() -> None:
    wait_for_admin()
    ensure_layout()
    bucket_ids = ensure_buckets()
    access_key_id, secret = ensure_key()
    ensure_grants(bucket_ids, access_key_id)
    # The emit contract (contracts/store-migration.md section "bootstrap"): gen_secrets
    # --record-garage parses exactly these two KEY=VALUE lines from the one-shot's stdout.
    print("=== GARAGE S3 KEY (record into .env: scripts/gen_secrets --record-garage) ===")
    print(f"GARAGE_ACCESS_KEY_ID={access_key_id}")
    print(f"GARAGE_SECRET_ACCESS_KEY={secret}")
    print("=== END GARAGE S3 KEY ===")
    print("garage-init: done")


if __name__ == "__main__":
    main()
