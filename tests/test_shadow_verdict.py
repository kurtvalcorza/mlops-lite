"""016 US2 — advisory shadow-replay verdict math (T312, FR-150, SC-095/097).

Champion (logged predictions) vs challenger (replayed predictions) over the same `(input, label)` window,
honouring the modality metric + direction. Always `advisory: True` — never gates.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shadow import FakeS3Del, load_shadow, make_quality  # noqa: E402

LLM = "text-generation"
ASR = "asr"


def _shadow():
    return load_shadow(make_quality(FakeS3Del()))


def _pairs(champ_preds, labels):
    return [{"prediction_id": f"p{i}", "input": "x", "label": lab, "champion_prediction": cp, "ts": i}
            for i, (cp, lab) in enumerate(zip(champ_preds, labels))]


def test_challenger_wins_higher_better():
    sh = _shadow()
    # task_accuracy (higher-better). champion 1/3 right, challenger 3/3 right.
    labels = ["paris", "7", "blue"]
    pairs = _pairs(["paris", "wrong", "wrong"], labels)
    verdict = sh.build_verdict("m", "1", "2", LLM, pairs, ["paris", "7", "blue"])
    assert verdict["metric"] == "task_accuracy" and verdict["direction"] == "higher"
    assert verdict["champion"]["value"] < verdict["challenger"]["value"]
    assert verdict["winner"] == "challenger" and verdict["advisory"] is True
    assert verdict["n_pairs"] == 3


def test_champion_wins_and_tie():
    sh = _shadow()
    labels = ["a", "b"]
    champ_better = sh.build_verdict("m", "1", "2", LLM, _pairs(["a", "b"], labels), ["a", "wrong"])
    assert champ_better["winner"] == "champion"
    tie = sh.build_verdict("m", "1", "2", LLM, _pairs(["a", "b"], labels), ["a", "b"])
    assert tie["winner"] == "tie"


def test_lower_better_metric_inverts_winner():
    sh = _shadow()
    # ASR → WER (lower-better). champion transcribes perfectly, challenger errs → champion wins.
    labels = ["the quick brown fox"]
    pairs = _pairs(["the quick brown fox"], labels)
    verdict = sh.build_verdict("m", "1", "2", ASR, pairs, ["the slow brown fox"])
    assert verdict["metric"] == "wer" and verdict["direction"] == "lower"
    assert verdict["winner"] == "champion"


def test_challenger_pred_count_mismatch_raises():
    sh = _shadow()
    pairs = _pairs(["a", "b"], ["a", "b"])
    try:
        sh.build_verdict("m", "1", "2", LLM, pairs, ["only-one"])
    except sh.ShadowError:
        pass
    else:
        raise AssertionError("expected ShadowError on a challenger/window length mismatch")


def test_run_replay_orchestration_persists_verdict():
    # ready window + injected challenger scorer → builds + persists an advisory verdict (read back).
    from _shadow import seed_input, seed_label, seed_prediction
    s3 = FakeS3Del()
    q = make_quality(s3, capture=True)
    sh = load_shadow(q)
    for i in range(4):
        pid = f"p{i}"
        seed_input(s3, q, LLM, pid, f"prompt{i}", float(i))
        seed_prediction(s3, q, pid, name="m", version=1, modality=LLM, prediction="wrong", ts=float(i))
        seed_label(s3, q, pid, "right")

    # challenger always predicts the label → perfect; champion always wrong → challenger wins.
    def scorer(pairs, modality, version):
        assert version == "9"
        return ["right"] * len(pairs)

    verdict = sh.run_replay("m", "1", "9", LLM, shadow_id="sid1", scorer=scorer, min_pairs=3)
    assert verdict["status"] == "completed" and verdict["winner"] == "challenger"
    assert verdict["advisory"] is True and verdict["shadow_id"] == "sid1"
    assert sh.read_verdict("sid1")["winner"] == "challenger"  # persisted + readable


def test_run_replay_insufficient_persists_status_no_scoring():
    from _shadow import seed_input, seed_label, seed_prediction
    s3 = FakeS3Del()
    q = make_quality(s3, capture=True)
    sh = load_shadow(q)
    seed_input(s3, q, LLM, "p0", "x", 1.0)
    seed_prediction(s3, q, "p0", name="m", version=1, modality=LLM, prediction="a", ts=1.0)
    seed_label(s3, q, "p0", "a")

    def scorer(pairs, modality, version):
        raise AssertionError("scorer must not run when the window is insufficient")

    res = sh.run_replay("m", "1", "9", LLM, shadow_id="sid2", scorer=scorer, min_pairs=20)
    assert res["status"] == "insufficient_data" and "winner" not in res
    assert sh.read_verdict("sid2")["status"] == "insufficient_data"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
