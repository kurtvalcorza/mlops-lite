"""012 US3 — best trial → register (only the winner) + study-id/params/metric tags (T235 / SC-073).

After a study completes, exactly ONE version (the best trial) survives, tagged with the study id +
winning hyperparameters + winning metric (so it flows into 011's gated promotion); every other trial's
version is deleted (no registry flood).
"""
import json
import sys

from _hpo import FakeClient, TrainRecorder, load_hpo

m = load_hpo()


def _score_by_version(name, version, modality):
    # higher version number → higher accuracy → the last trial wins (deterministic best).
    return {"metric": "task_accuracy", "value": float(version), "direction": "higher"}


def _req():
    return {"output_name": "toy-llm", "modality": "llm", "dataset_name": "d", "dataset_version": "1"}


def test_only_best_version_survives_losers_deleted():
    train = TrainRecorder("toy-llm")           # versions "1".."4" across 4 trials
    client = FakeClient()
    summary = m.run_study(_req(), train_fn=train, eval_fn=_score_by_version,
                          n_trials=4, client=client)
    best_version = summary["best"]["version"]
    # exactly one survivor: the winner is tagged, never deleted.
    assert (("toy-llm", best_version)) in client.tags
    assert ("toy-llm", best_version) not in client.deleted
    # every OTHER registered version was deleted (losers leave no surviving version).
    created = {str(i + 1) for i in range(4)}
    losers = created - {best_version}
    assert {v for (_, v) in client.deleted} == losers


def test_winner_tagged_with_study_params_and_metric():
    train = TrainRecorder("toy-llm")
    client = FakeClient()
    summary = m.run_study(_req(), train_fn=train, eval_fn=_score_by_version,
                          n_trials=3, client=client)
    best_version = summary["best"]["version"]
    tags = client.tags[("toy-llm", best_version)]
    assert tags["hpo_study"] == summary["study_id"]
    assert tags["hpo_best_metric"] == "task_accuracy"
    # winning hyperparameters are recorded as JSON (the actual sampled set).
    params = json.loads(tags["hpo_best_params"])
    assert set(params) == {"lora_r", "lora_alpha", "lr", "steps"}
    assert summary["best"]["params"] == params


def test_eval_failed_version_is_cleaned_up_not_kept():
    # a trial that trained (registered a version) but whose eval failed must NOT leave a surviving
    # version — it's deleted alongside the losers.
    def score(name, version, modality):
        if version == "1":
            raise RuntimeError("eval failed for the first candidate")
        return {"metric": "task_accuracy", "value": float(version), "direction": "higher"}

    train = TrainRecorder("toy-llm")
    client = FakeClient()
    summary = m.run_study(_req(), train_fn=train, eval_fn=score, n_trials=3, client=client)
    # version "1" trained then eval-failed → it must be among the deleted, not the survivor.
    assert ("toy-llm", "1") in client.deleted
    assert summary["best"]["version"] != "1"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
