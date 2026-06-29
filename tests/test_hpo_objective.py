"""012 US2 — per-modality search spaces + objective wired to 011's eval metric (T231 / SC-071, SC-072).

Two layers: the pure search-space sampler (each modality searches only its own knobs), and the study
objective (each trial's value is 011's eval scalar; Optuna's best trial is the best eval metric in the
metric's optimize direction).
"""
import sys

from _hpo import FakeClient, load_hpo, load_search_spaces

m = load_hpo()
ss = load_search_spaces()


class FakeTrial:
    """Records suggest_* calls so a test can assert which knobs a space searched, returning the first
    choice / low bound deterministically."""

    def __init__(self):
        self.suggested = {}

    def suggest_categorical(self, name, choices):
        self.suggested[name] = ("categorical", choices)
        return choices[0]

    def suggest_float(self, name, low, high, log=False):
        self.suggested[name] = ("float", low, high, log)
        return low

    def suggest_int(self, name, low, high, step=1):
        self.suggested[name] = ("int", low, high, step)
        return low


# --- search spaces (pure) -------------------------------------------------------------------------

def test_llm_space_searches_only_llm_knobs():
    t = FakeTrial()
    params = ss.sample("llm", t)
    assert set(params) == {"lora_r", "lora_alpha", "lr", "steps"}
    assert t.suggested["lr"][3] is True               # lr is loguniform
    assert t.suggested["lora_r"][0] == "categorical"
    # none of the vision-only knobs leaked in
    assert "epochs" not in params and "unfreeze_epochs" not in params


def test_vision_space_searches_only_vision_knobs():
    t = FakeTrial()
    params = ss.sample("vision", t)
    assert set(params) == {"lr", "epochs", "unfreeze_epochs"}
    assert "lora_r" not in params and "steps" not in params   # LLM-LoRA knobs absent (SC-072)


def test_overrides_replace_a_knob_range():
    t = FakeTrial()
    ss.sample("llm", t, overrides={"lora_r": ("categorical", [8])})
    assert t.suggested["lora_r"][1] == [8]            # the override narrowed the range


def test_unknown_modality_raises():
    try:
        ss.sample("nonsense", FakeTrial())
    except ss.SearchSpaceError:
        pass
    else:
        raise AssertionError("expected SearchSpaceError for an unknown modality")


def test_stub_spaces_present_for_asr_and_embeddings():
    assert "lr" in ss.space_for("asr") and "lr" in ss.space_for("embeddings")


# --- objective wired to the eval metric -----------------------------------------------------------

def test_best_trial_is_the_best_eval_metric_maximize():
    # eval value = the sampled lora_r; higher is better (task_accuracy) → Optuna should pick the
    # trial whose objective (eval scalar) is largest.
    seen = {}

    def train(req):
        seen[req["lora_r"]] = True
        return {"model": {"name": "toy-llm", "version": str(req["lora_r"])}}

    def score(name, version, modality):
        return {"metric": "task_accuracy", "value": float(version), "direction": "higher"}

    summary = m.run_study(
        {"output_name": "toy-llm", "modality": "llm", "dataset_name": "d", "dataset_version": "1"},
        train_fn=train, eval_fn=score, n_trials=8, client=FakeClient())
    assert summary["direction"] == "maximize"
    # the best trial's objective equals the eval scalar, and it's the max seen.
    best_val = summary["best"]["value"]
    completed = [t["value"] for t in summary["trials"] if t["status"] == "completed"]
    assert best_val == max(completed)


def test_lower_is_better_metric_minimizes():
    # a WER-like objective (lower better): the study must MINIMIZE — best trial is the smallest value.
    def train(req):
        return {"model": {"name": "toy-asr", "version": str(req["lora_r"])}}

    def score(name, version, modality):
        return {"metric": "wer", "value": float(version) / 100.0, "direction": "lower"}

    summary = m.run_study(
        {"output_name": "toy-asr", "modality": "asr", "dataset_name": "d", "dataset_version": "1"},
        train_fn=train, eval_fn=score, n_trials=6, client=FakeClient())
    assert summary["direction"] == "minimize"
    completed = [t["value"] for t in summary["trials"] if t["status"] == "completed"]
    assert summary["best"]["value"] == min(completed)


def test_optimize_direction_resolves_from_011_registry():
    # the integration path: direction comes from 011's metric registry by modality — maximize for
    # higher-better (accuracy), minimize for lower-better (WER). A wrong sign optimizes toward worse.
    assert m.optimize_direction("llm") == "maximize"
    assert m.optimize_direction("vision") == "maximize"
    assert m.optimize_direction("asr") == "minimize"


def test_eval_failure_makes_the_trial_fail_not_worst_valid():
    # if 011's eval errors for a candidate, that trial is FAILED (not scored worst-valid, FR-115).
    def train(req):
        return {"model": {"name": "toy-llm", "version": str(req["lora_r"])}}

    calls = {"n": 0}

    def score(name, version, modality):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("011 eval blew up")
        return {"metric": "task_accuracy", "value": 0.5, "direction": "higher"}

    summary = m.run_study(
        {"output_name": "toy-llm", "modality": "llm", "dataset_name": "d", "dataset_version": "1"},
        train_fn=train, eval_fn=score, n_trials=3, client=FakeClient())
    assert any(t["status"] == "eval_failed" for t in summary["trials"])
    assert summary["completed"] == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
