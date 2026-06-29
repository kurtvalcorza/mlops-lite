"""011 US3 — offline champion-challenger (T218 / SC-068).

compare() scores the `@serving` champion and a challenger on the same held-out benchmark and declares
a per-metric winner. The non-negotiable invariant (Principle II): the two models are loaded
**sequentially** — at most one resident at any instant. The injected predict_fn doubles as a VRAM
residency monitor that fails if a second "load" ever overlaps the first, locking the mutex against a
future refactor to concurrent scoring.
"""
import sys

from _eval import FakeClient, FakeMV, load_evaluation

m = load_evaluation()
VIS = "image-classification"


class ResidencyMonitor:
    """Stands in for the serving path: each call models a sequential load→score→release of ONE model.
    Records the load order and asserts residency never exceeds 1 (the VRAM mutex)."""

    def __init__(self, scores):
        self.scores = scores          # version -> the label the "model" predicts for every row
        self.resident = 0
        self.max_resident = 0
        self.order = []

    def __call__(self, rows, modality, version):
        self.resident += 1
        self.max_resident = max(self.max_resident, self.resident)
        self.order.append(version)
        assert self.resident == 1, "two models resident at once — VRAM mutex violated"
        try:
            return [self.scores[version]] * len(rows)  # uniform prediction → deterministic accuracy
        finally:
            self.resident -= 1


def _client():
    return FakeClient({
        ("clf", "1"): FakeMV({"task": VIS}, run_id="r1"),   # champion @serving
        ("clf", "2"): FakeMV({"task": VIS}, run_id="r2"),   # challenger
    })


def test_challenger_wins_when_more_accurate(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    # benchmark's first label is the ground-truth the monitor must hit to "be correct".
    truth = m.load_benchmark(VIS).rows[0]["label"]
    wrong = "definitely-not-a-label"
    mon = ResidencyMonitor({"1": wrong, "2": truth})  # champion always wrong, challenger sometimes right

    res = m.compare("clf", "2", predict_fn=mon, client=_client())
    assert res["metric"] == "accuracy" and res["direction"] == m.HIGHER
    assert res["challenger"]["value"] >= res["champion"]["value"]
    assert res["winner"] == "challenger"
    # sequential, never concurrent: champion loaded and released before the challenger (SC-068).
    assert mon.max_resident == 1
    assert mon.order == ["1", "2"]


def test_champion_wins_and_tie(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    truth = m.load_benchmark(VIS).rows[0]["label"]
    champ_better = ResidencyMonitor({"1": truth, "2": "wrong"})
    assert m.compare("clf", "2", predict_fn=champ_better, client=_client())["winner"] == "champion"

    same = ResidencyMonitor({"1": truth, "2": truth})
    assert m.compare("clf", "2", predict_fn=same, client=_client())["winner"] == "tie"


def test_lower_is_better_winner_direction(monkeypatch):
    """A lower-better metric must invert the winner test — the lower scorer wins."""
    monkeypatch.setattr(m, "_serving_version", lambda c, name: "1")
    client = FakeClient({
        ("asr", "1"): FakeMV({"task": "asr"}, run_id="r1"),
        ("asr", "2"): FakeMV({"task": "asr"}, run_id="r2"),
    })
    # WER benchmark of one item: champion transcribes perfectly (WER 0), challenger errs (WER>0).
    rows = [{"audio_b64": "x", "text": "the quick brown fox"}]
    monkeypatch.setattr(m, "load_benchmark", lambda modality, ref=None: m.Benchmark("asr/x", "h", rows))

    def predict(rows, modality, version):
        return ["the quick brown fox"] if version == "1" else ["the slow brown fox"]

    res = m.compare("asr", "2", predict_fn=predict, client=client)
    assert res["metric"] == "wer" and res["direction"] == m.LOWER
    assert res["winner"] == "champion"  # champion's lower WER wins


def test_compare_requires_a_champion(monkeypatch):
    monkeypatch.setattr(m, "_serving_version", lambda c, name: None)
    try:
        m.compare("clf", "2", predict_fn=ResidencyMonitor({}), client=_client())
    except m.EvalError:
        pass
    else:
        raise AssertionError("expected EvalError when there is no @serving champion")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
