"""011 US3 champion-challenger, corrected by 015 (SC-090, FR-142).

After 015 every version is scored **at registration**, so `compare()` declares a winner by reading the
champion's and challenger's **logged eval metrics** — a pure metric lookup, no model reload. This closes
the SC-068 mislabel where the old live path scored whichever model the serving daemon happened to hold
(so both legs hit the same resident model and the comparison was degenerate). Loading no model trivially
preserves the one-model-in-VRAM invariant (Principle II): the residency monitor below proves the
predictor is **never** called at all.
"""
import sys

from _eval import FakeClient, FakeMV, load_evaluation

m = load_evaluation()
VIS = "image-classification"


def _ev_tags(metric, value, direction, modality, benchmark="vision/shapes_smoke.jsonl", h="hash0"):
    """The version tags read_eval reads back — what score-at-registration (015) logged on the version."""
    return {"task": modality, m.TAG_METRIC: metric, m.TAG_VALUE: str(value),
            m.TAG_DIRECTION: direction, m.TAG_MODALITY: modality,
            m.TAG_BENCHMARK: benchmark, m.TAG_BENCHMARK_HASH: h}


class NeverCalled:
    """A predictor that fails the test if compare ever loads/scores a model — compare must read logged
    metrics only (no reload, SC-090)."""

    def __call__(self, *a, **k):
        raise AssertionError("compare loaded a model — it must read logged metrics only (SC-090)")


def _client(champ_val, chall_val, *, metric="accuracy", direction=m.HIGHER, modality=VIS,
            champ_hash="hash0", chall_hash="hash0"):
    return FakeClient({
        ("clf", "1"): FakeMV(_ev_tags(metric, champ_val, direction, modality, h=champ_hash), run_id="r1"),
        ("clf", "2"): FakeMV(_ev_tags(metric, chall_val, direction, modality, h=chall_hash), run_id="r2"),
    })


def test_challenger_wins_from_logged_metrics(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    res = m.compare("clf", "2", predict_fn=NeverCalled(), client=_client(0.5, 0.9))
    assert res["metric"] == "accuracy" and res["direction"] == m.HIGHER
    assert res["champion"]["value"] == 0.5 and res["challenger"]["value"] == 0.9
    assert res["winner"] == "challenger" and res["delta"] == round(0.9 - 0.5, 6)
    assert res["benchmark_mismatch"] is False


def test_champion_wins_and_tie(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    assert m.compare("clf", "2", client=_client(0.9, 0.4))["winner"] == "champion"
    assert m.compare("clf", "2", client=_client(0.7, 0.7))["winner"] == "tie"


def test_lower_is_better_inverts_winner(monkeypatch):
    """A lower-better metric (WER) must invert the winner test — the lower scorer wins."""
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = _client(0.1, 0.4, metric="wer", direction=m.LOWER, modality="asr")
    res = m.compare("clf", "2", client=client)  # name is arbitrary here; only the modality tag matters
    assert res["metric"] == "wer" and res["direction"] == m.LOWER
    assert res["winner"] == "champion"  # champion's lower WER wins


def test_benchmark_mismatch_flagged(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    res = m.compare("clf", "2", client=_client(0.5, 0.9, champ_hash="hA", chall_hash="hB"))
    assert res["benchmark_mismatch"] is True  # scored on different bytes — surfaced, not silent


def test_metric_mismatch_refuses(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = FakeClient({
        ("clf", "1"): FakeMV(_ev_tags("accuracy", 0.8, m.HIGHER, VIS), run_id="r1"),
        ("clf", "2"): FakeMV(_ev_tags("wer", 0.1, m.LOWER, "asr"), run_id="r2"),
    })
    try:
        m.compare("clf", "2", client=client)
    except m.EvalError:
        pass
    else:
        raise AssertionError("expected EvalError on a metric/modality mismatch")


def test_missing_metric_refuses(monkeypatch):
    """A version with no logged metric can't be compared — 015 should have scored it at registration."""
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = FakeClient({
        ("clf", "1"): FakeMV(_ev_tags("accuracy", 0.8, m.HIGHER, VIS), run_id="r1"),
        ("clf", "2"): FakeMV({"task": VIS}, run_id="r2"),  # registered but unscored
    })
    try:
        m.compare("clf", "2", client=client)
    except m.EvalError:
        pass
    else:
        raise AssertionError("expected EvalError when the challenger has no logged metric")


def test_compare_requires_a_champion(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: None)
    try:
        m.compare("clf", "2", client=_client(0.5, 0.9))
    except m.EvalError:
        pass
    else:
        raise AssertionError("expected EvalError when there is no @serving champion")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
