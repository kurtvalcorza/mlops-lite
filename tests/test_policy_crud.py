"""018 US3 — policy declaration validation + CRUD store (T366/T367, FR-179).

Offline, GPU-free: the contract validation (structural, write-time, structured errors — the spec
edge case "policy misconfiguration is rejected at declaration time, never discovered at breach
time") plus the MinIO-backed store with a fake S3.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from platformlib.contracts import ContractError, ModelPolicy  # noqa: E402


def _doc(**over):
    doc = {
        "model_name": "vision-mobilenet",
        "modality": "vision",
        "monitors": [{"kind": "quality", "window_n": 50, "drop_pct": 0.1}],
        "check_interval_s": 900,
        "on_breach": {"action": "retrain", "dataset": "latest",
                      "params": {"dataset_name": "shapes-live"}},
        "promotion_mode": "suggest",
        "enabled": True,
        "updated_at": 1.0,
        "updated_by": "operator",
    }
    doc.update(over)
    return doc


def _errors(doc):
    try:
        ModelPolicy.from_json(doc)
    except ContractError as e:
        return {err["field"] for err in json.loads(str(e))["errors"]}
    return set()


def test_valid_policy_passes():
    assert _errors(_doc()) == set()


def test_unknown_modality_rejected():
    assert "modality" in _errors(_doc(modality="tabular"))   # no fine-tune flow → rejected
    assert "modality" in _errors(_doc(modality="banana"))


def test_interval_and_monitors_rejected():
    assert "check_interval_s" in _errors(_doc(check_interval_s=0))
    assert "check_interval_s" in _errors(_doc(check_interval_s=59))
    assert "monitors" in _errors(_doc(monitors=[]))
    assert "monitors[0].kind" in _errors(_doc(monitors=[{"kind": "vibes"}]))


def test_input_drift_needs_a_reference():
    errs = _errors(_doc(monitors=[{"kind": "input_drift"}]))
    assert "monitors[0].reference" in errs
    ok = _doc(monitors=[{"kind": "input_drift",
                         "reference": {"name": "feat-baseline", "version": "abc123"}}])
    assert _errors(ok) == set()


def test_breach_action_needs_dataset_name():
    assert "on_breach.params.dataset_name" in _errors(
        _doc(on_breach={"action": "retrain", "dataset": "latest", "params": {}}))
    assert "on_breach.dataset" in _errors(
        _doc(on_breach={"action": "retrain", "params": {"dataset_name": "d"}}))
    assert "on_breach.action" in _errors(_doc(on_breach={"action": "email-someone"}))


def test_promotion_mode_rejected():
    assert "promotion_mode" in _errors(_doc(promotion_mode="yolo"))


# --- the store, against a fake S3 -------------------------------------------------------------------

from _quality import FakeS3  # noqa: E402 — the shared in-memory S3 fake

from app import policies  # noqa: E402


def _fake_store():
    fake = FakeS3()
    policies._s3 = lambda: fake
    # FakeS3 lacks delete_object — add it so delete/clear paths work
    fake.delete_object = lambda Bucket, Key: fake.objs.pop(Key, None)
    return fake


def test_put_get_list_delete_roundtrip():
    _fake_store()
    stored = policies.put_policy("vision-mobilenet", _doc())
    assert stored["model_name"] == "vision-mobilenet" and stored["updated_at"] > 0
    assert policies.get_policy("vision-mobilenet")["modality"] == "vision"
    policies.put_policy("qa-demo", _doc(model_name="qa-demo", modality="llm",
                                        on_breach={"action": "retrain", "dataset": "latest",
                                                   "params": {"dataset_name": "qa-live"}}))
    assert [p["model_name"] for p in policies.list_policies()] == ["qa-demo", "vision-mobilenet"]
    policies.delete_policy("qa-demo")
    assert policies.get_policy("qa-demo") is None
    assert len(policies.list_policies()) == 1


def test_invalid_policy_is_rejected_and_never_stored():
    _fake_store()
    try:
        policies.put_policy("m", _doc(model_name="m", modality="nope"))
    except policies.PolicyError as e:
        assert "modality" in str(e)
    else:
        raise AssertionError("expected PolicyError")
    assert policies.get_policy("m") is None                  # nothing stored (FR-179)


def test_pending_and_status_do_not_pollute_the_policy_list():
    _fake_store()
    policies.put_policy("vision-mobilenet", _doc())
    policies.save_pending({"model_name": "vision-mobilenet",
                           "breach": {"signal": "quality", "score": 0.4, "at": 1.0},
                           "attempts": 1, "next_attempt_at": 2.0})
    policies.save_status("vision-mobilenet", {"last_check_at": 1.0})
    assert len(policies.list_policies()) == 1                # sub-prefixes filtered out
    st = policies.policy_status("vision-mobilenet")
    assert st["pending_retrain"]["attempts"] == 1
    policies.clear_pending("vision-mobilenet")
    assert policies.policy_status("vision-mobilenet")["pending_retrain"] is None


def test_suggestion_lifecycle():
    _fake_store()
    rec = policies.create_suggestion("qa-demo", "7", {"verdict": "pass"},
                                     {"winner": "challenger"})
    assert rec["state"] == "open"
    assert policies.list_suggestions(state="open")[0]["id"] == rec["id"]
    done = policies.resolve_suggestion(rec["id"], "accepted")
    assert done["state"] == "accepted" and done["resolved_at"] is not None
    try:
        policies.resolve_suggestion(rec["id"], "dismissed")
    except policies.PolicyError:
        pass
    else:
        raise AssertionError("resolving twice must be refused")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
