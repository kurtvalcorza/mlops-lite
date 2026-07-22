"""Ordering invariant of the go-live route (023 grill): an unresolvable text-generation
adapter is refused BEFORE the @serving alias / serving pointer moves (FR-265).

This is the one ordering leg of `POST /models/{name}/promote` that is deterministic, GPU-free,
and otherwise only reachable through the HTTP stack. The refusal is a registry-level resolution
check (`registry.llm_target_info` -> gateway/app/routers/models.py:111-113) that runs AHEAD of the
gate and `activation.service().activate()`, so a bad adapter must never move the alias. The sibling
ordering legs are covered off-HTTP already: `assert_no_conflict` ordering in test_activation.py and
the gate's block/warn/pass matrix in test_promotion_gate.py.

Requires the stack up (`make up` / serve_up.ps1); conftest's live guard skips it offline.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from _auth import auth_headers

GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
LLM_TASK = "text-generation"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GW + path, data=data,
        headers=auth_headers({"Content-Type": "application/json"}), method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _serving_version(name):
    """The promoted @serving version for `name`, or None when nothing is promoted."""
    s, body = _req("GET", f"/models/{name}")
    return (body.get("serving") or {}).get("version") if s == 200 else None


def main() -> int:
    name = os.getenv("PROMOTE_ORDER_MODEL", "promote-order-adapter")

    # A text-generation LoRA adapter with NO base_model lineage: llm_target_info() returns an
    # {error: ...}, so the route must refuse (409) BEFORE registry.promote moves the alias.
    s, reg = _req("POST", "/models", {
        "name": name, "source": "s3://m/bad-adapter",
        "tags": {"task": LLM_TASK, "kind": "lora-adapter"},
    })
    if s != 201 or "version" not in reg:
        print(f"[FAIL] setup register -> {s} {reg}")
        return 1
    version = reg["version"]

    before = _serving_version(name)  # capture the serving pointer BEFORE the refused promote

    sp, pr = _req("POST", f"/models/{name}/promote", {"version": version})
    if sp != 409 or "refused" not in json.dumps(pr):
        print(f"[FAIL] expected 409 promotion refusal, got {sp} {pr}")
        return 1
    if pr.get("promoted") is True or "serving_llm" in pr:
        print(f"[FAIL] a refusal must not switch the served LLM -> {pr}")
        return 1

    after = _serving_version(name)  # ...and AFTER — the alias must not have moved
    if before != after:
        print(f"[FAIL] alias moved on a refused promote: {before!r} -> {after!r}")
        return 1

    print(f"[OK] unresolvable adapter v{version} refused before the alias moved (FR-265)")
    return 0


def test_promote_ordering(require_gateway, require_key):
    """Skips cleanly offline via the guard fixtures; runs against a live keyed stack."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
