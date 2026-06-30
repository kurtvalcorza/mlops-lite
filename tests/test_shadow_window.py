"""016 US2 — replay window resolution (T311, FR-148/149).

The window is `captured ∩ labeled ∩ champion-scorable`: a captured input joins only when the champion
(@serving version) logged a non-None prediction for it AND a label exists. Streamed (prediction=None),
another version's rows, unlabeled, and uncaptured records all fall out. Newest `window_n` selected.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shadow import FakeS3Del, load_shadow, make_quality, seed_input, seed_label, seed_prediction  # noqa: E402

LLM = "text-generation"


def _setup(**flags):
    s3 = FakeS3Del()
    q = make_quality(s3, **flags)
    sh = load_shadow(q)
    return s3, q, sh


def test_window_joins_captured_labeled_champion_scorable():
    s3, q, sh = _setup()
    # champion = clf@v1. Three captured+labeled+scored pairs, ascending ts.
    for i, ts in enumerate([10.0, 20.0, 30.0]):
        pid = f"p{i}"
        seed_input(s3, q, LLM, pid, f"prompt{i}", ts)
        seed_prediction(s3, q, pid, name="clf", version=1, modality=LLM, prediction=f"ans{i}", ts=ts)
        seed_label(s3, q, pid, f"ans{i}")
    pairs = sh.resolve_window("clf", 1, LLM, window_n=100)
    assert [p["prediction_id"] for p in pairs] == ["p0", "p1", "p2"]  # oldest→newest
    assert pairs[0]["input"] == "prompt0" and pairs[0]["champion_prediction"] == "ans0"
    assert pairs[0]["label"] == "ans0"


def test_excludes_streamed_unlabeled_and_other_version():
    s3, q, sh = _setup()
    # good pair
    seed_input(s3, q, LLM, "good", "p", 5.0)
    seed_prediction(s3, q, "good", name="clf", version=1, modality=LLM, prediction="a", ts=5.0)
    seed_label(s3, q, "good", "a")
    # streamed: prediction None → excluded
    seed_input(s3, q, LLM, "streamed", "p", 6.0)
    seed_prediction(s3, q, "streamed", name="clf", version=1, modality=LLM, prediction=None, ts=6.0)
    seed_label(s3, q, "streamed", "a")
    # unlabeled → excluded
    seed_input(s3, q, LLM, "nolabel", "p", 7.0)
    seed_prediction(s3, q, "nolabel", name="clf", version=1, modality=LLM, prediction="a", ts=7.0)
    # served by a different version → excluded
    seed_input(s3, q, LLM, "otherver", "p", 8.0)
    seed_prediction(s3, q, "otherver", name="clf", version=2, modality=LLM, prediction="a", ts=8.0)
    seed_label(s3, q, "otherver", "a")
    pairs = sh.resolve_window("clf", 1, LLM, window_n=100)
    assert [p["prediction_id"] for p in pairs] == ["good"]


def test_window_keeps_only_newest_window_n():
    s3, q, sh = _setup()
    for i in range(5):
        pid = f"p{i}"
        seed_input(s3, q, LLM, pid, f"prompt{i}", float(i))
        seed_prediction(s3, q, pid, name="m", version=1, modality=LLM, prediction="a", ts=float(i))
        seed_label(s3, q, pid, "a")
    pairs = sh.resolve_window("m", 1, LLM, window_n=2)
    assert [p["prediction_id"] for p in pairs] == ["p3", "p4"]  # newest 2, oldest→newest


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
