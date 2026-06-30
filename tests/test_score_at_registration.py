"""015 US1 — score-at-registration glue (T285): score_and_log logs a version's primary metric.

Offline + GPU-free: load `training/scoring/__init__.py` in isolation, point its `_load_evaluation` at
the same isolated 011 harness the other eval tests use (tests/_eval.py), stub `load_benchmark` to a tiny
in-memory fixture, and inject a per-modality `predict_fn` (no torch, no llama.cpp, no model). Asserts
each modality logs the right metric + direction + provenance tags against the new version — the contract
the gate/compare/HPO read back. The on-hardware scorers (real llama-server/whisper-cli/torch) are
exercised by SC-087/088/092 on the GPU box.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _eval import FakeClient, FakeMV, load_evaluation  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_scoring(ev):
    """A fresh, isolated copy of training/scoring with its eval harness pinned to the offline `ev`."""
    spec = importlib.util.spec_from_file_location(
        "scoring_under_test", os.path.join(REPO, "training", "scoring", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._load_evaluation = lambda: ev
    return mod


class _Bench:
    def __init__(self, name, rows):
        self.name, self.digest, self.rows = name, "deadbeefcafef00d", rows


def _pin_benchmark(ev, name, rows):
    ev.load_benchmark = lambda modality, ref=None: _Bench(name, rows)


def _tags(client, name, version):
    return client.versions[(name, str(version))].tags


def test_llm_score_logs_task_accuracy():
    ev = load_evaluation()
    rows = [{"prompt": "capital of France?", "answer": "Paris"},
            {"prompt": "2+2?", "answer": "4"}]
    _pin_benchmark(ev, "llm/qa_smoke.jsonl", rows)
    scoring = _load_scoring(ev)
    c = FakeClient({("llm", "5"): FakeMV(tags={"task": "text-generation"})})

    # one right, one wrong -> task_accuracy 0.5 (higher-better)
    def predict_fn(rows, modality, version):
        assert version == "5"
        return ["the answer is Paris", "5"]

    res = scoring.score_and_log("llm", "5", "text-generation", predict_fn, client=c)
    assert res["metric"] == "task_accuracy" and res["direction"] == "higher"
    assert res["value"] == 0.5
    t = _tags(c, "llm", "5")
    assert t["eval_metric"] == "task_accuracy" and t["eval_value"] == "0.5"
    assert t["eval_direction"] == "higher" and t["eval_benchmark"] == "llm/qa_smoke.jsonl"
    assert t["eval_benchmark_hash"] == "deadbeefcafef00d"


def test_vision_score_logs_accuracy():
    ev = load_evaluation()
    rows = [{"image_b64": "x", "label": "red"}, {"image_b64": "y", "label": "blue"}]
    _pin_benchmark(ev, "vision/shapes_smoke.jsonl", rows)
    scoring = _load_scoring(ev)
    c = FakeClient({("vis", "2"): FakeMV(tags={"task": "image-classification"})})

    res = scoring.score_and_log("vis", "2", "image-classification",
                                lambda r, m, v: ["red", "blue"], client=c)
    assert res["metric"] == "accuracy" and res["value"] == 1.0 and res["direction"] == "higher"
    assert _tags(c, "vis", "2")["eval_value"] == "1.0"


def test_asr_score_logs_wer_lower_better():
    ev = load_evaluation()
    rows = [{"audio_b64": "a", "text": "hello world"}, {"audio_b64": "b", "text": "open the registry"}]
    _pin_benchmark(ev, "asr/wer_smoke.jsonl", rows)
    scoring = _load_scoring(ev)
    c = FakeClient({("asr", "1"): FakeMV(tags={"task": "asr"})})

    # first clip perfect, second has one substitution out of three words -> WER = 1/5 words
    res = scoring.score_and_log("asr", "1", "asr",
                                lambda r, m, v: ["hello world", "open the directory"], client=c)
    assert res["metric"] == "wer" and res["direction"] == "lower"
    assert res["value"] == round(1 / 5, 6)
    assert _tags(c, "asr", "1")["eval_direction"] == "lower"


def test_embedding_score_logs_recall_at_k():
    ev = load_evaluation()
    rows = [{"query": f"q{i}", "positive": f"p{i}"} for i in range(6)]
    _pin_benchmark(ev, "embedding/recall_smoke.jsonl", rows)
    scoring = _load_scoring(ev)
    c = FakeClient({("emb", "4"): FakeMV(tags={"task": "embedding"})})

    # a perfect ranker puts each query's own positive (index i) first -> recall@k = 1.0
    def predict_fn(rows, modality, version):
        n = len(rows)
        return [[i] + [j for j in range(n) if j != i] for i in range(n)]

    res = scoring.score_and_log("emb", "4", "embedding", predict_fn, client=c)
    assert res["metric"] == "recall_at_k" and res["value"] == 1.0 and res["direction"] == "higher"

    # a ranker that buries each positive past k=5 misses every query -> recall@5 = 0.0
    def bad_predict(rows, modality, version):
        n = len(rows)
        return [[j for j in range(n) if j != i] + [i] for i in range(n)]

    res0 = scoring.score_and_log("emb", "4", "embedding", bad_predict, client=c, log=False)
    assert res0["value"] == 0.0


def test_scoring_no_log_does_not_touch_client():
    ev = load_evaluation()
    rows = [{"prompt": "p", "answer": "a"}]
    _pin_benchmark(ev, "llm/qa_smoke.jsonl", rows)
    scoring = _load_scoring(ev)
    c = FakeClient({("llm", "9"): FakeMV(tags={"task": "text-generation"})})

    scoring.score_and_log("llm", "9", "text-generation", lambda r, m, v: ["a"], client=c, log=False)
    assert "eval_metric" not in _tags(c, "llm", "9")  # log=False writes no tags
