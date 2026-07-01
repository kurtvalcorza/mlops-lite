"""016 US3 — graceful insufficient/no-corpus handling (T317, FR-152, SC-098).

`prepare()` guards the replay: capture off → `no_corpus`; capture on but nothing captured for the
modality → `inputs_not_captured`; fewer than `MIN_PAIRS` captured∩labeled pairs → `insufficient_data`;
an out-of-scope modality → a clear refusal. Never a misleading verdict from thin data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shadow import FakeS3Del, load_shadow, make_quality, seed_input, seed_label, seed_prediction  # noqa: E402

LLM = "text-generation"


def _setup(**flags):
    s3 = FakeS3Del()
    q = make_quality(s3, **flags)
    return s3, q, load_shadow(q)


def test_capture_off_is_no_corpus():
    _, _, sh = _setup(capture=False)
    res = sh.prepare("m", "1", LLM)
    assert res["status"] == "no_corpus"


def test_no_captured_inputs_for_modality():
    _, _, sh = _setup(capture=True)
    res = sh.prepare("m", "1", LLM)
    assert res["status"] == "inputs_not_captured"


def test_too_few_pairs_is_insufficient_data():
    s3, q, sh = _setup(capture=True)
    # only 2 captured∩labeled pairs but MIN_PAIRS default is 20 → insufficient
    for i in range(2):
        pid = f"p{i}"
        seed_input(s3, q, LLM, pid, "x", float(i))
        seed_prediction(s3, q, pid, name="m", version=1, modality=LLM, prediction="a", ts=float(i))
        seed_label(s3, q, pid, "a")
    res = sh.prepare("m", "1", LLM, min_pairs=20)
    assert res["status"] == "insufficient_data" and res["n_pairs"] == 2 and res["min"] == 20


def test_ready_when_enough_pairs():
    s3, q, sh = _setup(capture=True)
    for i in range(5):
        pid = f"p{i}"
        seed_input(s3, q, LLM, pid, "x", float(i))
        seed_prediction(s3, q, pid, name="m", version=1, modality=LLM, prediction="a", ts=float(i))
        seed_label(s3, q, pid, "a")
    res = sh.prepare("m", "1", LLM, min_pairs=3)
    assert res["status"] == "ready" and res["n_pairs"] == 5 and len(res["pairs"]) == 5


def test_out_of_scope_modality_refused():
    _, _, sh = _setup(capture=True)
    try:
        sh.prepare("m", "1", "embedding")
    except sh.ShadowError:
        pass
    else:
        raise AssertionError("expected ShadowError for an out-of-scope modality (embedding)")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
