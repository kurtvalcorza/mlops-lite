"""018 T362.1 — FR-176 seam retirement (offline, GPU-free).

Pins the bounded FR-176 change: the shared S3 leaf helpers live in `platformlib.s3io` (and
`datasets` re-exports them), and the four training seams reach the gateway's evaluation/batch/shadow
cores through the ONE audited `platformlib.gateway_bridge` instead of each hand-rolling
`sys.path.insert(0, gateway/)`.
"""
import inspect
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
if os.path.join(REPO, "training") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "training"))

from platformlib import gateway_bridge, s3io  # noqa: E402


# ---- s3io: the extracted S3 leaf helpers ------------------------------------------------------
def test_s3io_constants_default():
    assert s3io.BUCKET == os.getenv("DATASETS_BUCKET", "datasets")
    assert s3io.S3_ENDPOINT.startswith("http")


def test_s3io_client_builds_with_env_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    client = s3io._s3()                       # boto3 does not connect at construction
    assert client.meta.endpoint_url == s3io.S3_ENDPOINT


def test_s3io_client_missing_creds_raises_by_name(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    with pytest.raises(KeyError):             # names the var, never its value (FR-017)
        s3io._s3()


def test_datasets_reexports_the_same_s3io_objects():
    gateway_bridge._ensure_gateway_on_path()  # make `app.*` importable (the bridge's one job)
    from app import datasets
    assert datasets._s3 is s3io._s3          # re-export, not a copy — one client factory
    assert datasets.BUCKET == s3io.BUCKET


# ---- gateway_bridge: the one audited injection ------------------------------------------------
def test_bridge_returns_the_gateway_cores():
    assert gateway_bridge.evaluation().__name__ == "app.evaluation"
    assert gateway_bridge.batch().__name__ == "app.batch"
    assert gateway_bridge.shadow().__name__ == "app.shadow"


def test_bridge_injects_gateway_once_idempotent():
    gw = gateway_bridge._GATEWAY
    # calling twice must not stack duplicate entries
    gateway_bridge._ensure_gateway_on_path()
    gateway_bridge._ensure_gateway_on_path()
    assert sys.path.count(gw) == 1


# ---- the four seams now delegate to the bridge (no per-seam gateway injection) -----------------
def test_seams_return_the_bridge_cores():
    from flows.batch_infer import _load_batch
    from flows.hpo import _load_evaluation as hpo_eval
    from flows.shadow_replay import _load_gateway_shadow
    from scoring import _load_evaluation as scoring_eval

    assert scoring_eval() is gateway_bridge.evaluation()
    assert hpo_eval() is gateway_bridge.evaluation()
    assert _load_batch() is gateway_bridge.batch()
    assert _load_gateway_shadow() is gateway_bridge.shadow()


def test_seams_no_longer_hardcode_a_gateway_path_injection():
    # FR-176: the retired seams must not rebuild `os.path.join(..., "gateway")` + sys.path.insert.
    from flows import batch_infer, hpo, shadow_replay
    from scoring import _load_evaluation as scoring_eval

    for fn in (scoring_eval, hpo._load_evaluation, batch_infer._load_batch,
               shadow_replay._load_gateway_shadow):
        src = inspect.getsource(fn)
        assert "sys.path.insert" not in src, f"{fn.__qualname__} still injects sys.path"
        assert '"gateway"' not in src, f"{fn.__qualname__} still hardcodes the gateway dir"
        assert "gateway_bridge" in src, f"{fn.__qualname__} does not use the platformlib bridge"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
