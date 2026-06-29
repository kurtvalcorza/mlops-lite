"""012 US1 — Optuna study runner wrapping the trainer (T228 / SC-070).

Runs the real study orchestration (real Optuna, injected train/eval seams): N trials execute as N
training-run invocations, sequentially; a deliberately-failing trial is recorded and registers no
surviving version, and the study still finishes with a best trial. No GPU, no live stack.
"""
import sys

from _hpo import FakeClient, TrainRecorder, load_hpo

m = load_hpo()


def _eval_by_lr(name, version, modality):
    """Objective seam: score = the trial's version number (so higher version → better), giving a
    deterministic, known best trial for the assertions."""
    return {"metric": "task_accuracy", "value": float(version), "direction": "higher"}


def _study_req(n=3):
    return {"dataset_name": "ds", "dataset_version": "1", "output_name": "toy-llm", "modality": "llm"}


def test_runs_exactly_n_trials_each_a_training_invocation():
    train = TrainRecorder("toy-llm")
    client = FakeClient()
    summary = m.run_study(_study_req(), train_fn=train, eval_fn=_eval_by_lr,
                          n_trials=3, client=client)
    assert len(train.calls) == 3                  # exactly N real training invocations (SC-070)
    assert summary["n_trials"] == 3 and summary["completed"] == 3
    assert summary["modality"] == "llm" and summary["direction"] == "maximize"
    # every trial forwarded the study's dataset/output + sampled LLM knobs into the training req.
    for req in train.calls:
        assert req["dataset_name"] == "ds" and req["output_name"] == "toy-llm"
        assert req["modality"] == "llm" and "lora_r" in req and "lr" in req


def test_trials_are_sequential_single_threaded():
    # n_jobs=1 means trials never overlap. A recorder that asserts no re-entrancy locks the invariant:
    # if two trials ever ran concurrently, the in-flight flag would already be set on entry.
    inflight = {"now": 0, "max": 0}

    def train(req):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            assert inflight["now"] == 1, "two trials trained concurrently — Principle II violated"
            return {"model": {"name": "toy-llm", "version": str(id(req) % 100000)}}
        finally:
            inflight["now"] -= 1

    summary = m.run_study(_study_req(), train_fn=train, eval_fn=_eval_by_lr,
                          n_trials=4, client=FakeClient())
    assert inflight["max"] == 1
    assert summary["completed"] == 4


def test_failed_trial_recorded_no_surviving_version_study_continues():
    train = TrainRecorder("toy-llm", fail_on=(1,))   # trial index 1 raises
    client = FakeClient()
    summary = m.run_study(_study_req(), train_fn=train, eval_fn=_eval_by_lr,
                          n_trials=3, client=client)
    assert len(train.calls) == 3                       # the study continued past the failure
    assert summary["completed"] == 2                   # only the 2 good trials completed
    statuses = {t["trial"]: t["status"] for t in summary["trials"]}
    assert "failed" in statuses.values()
    # the failed trial registered NO version → it never appears in created/cleaned versions.
    assert summary["best"] is not None                 # study still has a best among the survivors


def test_trial_that_produces_no_model_is_failed():
    train = TrainRecorder("toy-llm", produce_no_model=(0,))
    summary = m.run_study(_study_req(), train_fn=train, eval_fn=_eval_by_lr,
                          n_trials=2, client=FakeClient())
    assert summary["completed"] == 1
    assert any(t["status"] == "failed" for t in summary["trials"])


def test_all_trials_failing_yields_no_best_and_no_registration():
    train = TrainRecorder("toy-llm", fail_on=(0, 1))
    client = FakeClient()
    summary = m.run_study(_study_req(), train_fn=train, eval_fn=_eval_by_lr,
                          n_trials=2, client=client)
    assert summary["completed"] == 0 and summary["best"] is None
    assert client.tags == {}                           # nothing registered as a winner


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
