"""Offline harness for the 012 HPO tests.

Loads `training/flows/hpo.py` and `training/search_spaces.py` with a stubbed `mlflow` (the study runner
opens a parent + nested child runs but the tests don't need a live tracking server) and a fake MLflow
registry client (records winner tags + loser deletions). The two heavy seams — training + 011 eval —
are injected, so the study orchestration runs with **real Optuna** and zero GPU.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAINING = os.path.join(REPO, "training")


def _stub_mlflow():
    if "mlflow" in sys.modules and getattr(sys.modules["mlflow"], "_hpo_stub", False):
        return

    class _Run:
        def __init__(self):
            self.info = types.SimpleNamespace(run_id="run-stub")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    m = types.ModuleType("mlflow")
    m._hpo_stub = True
    m.set_tracking_uri = lambda *a, **k: None
    m.set_experiment = lambda *a, **k: None
    m.start_run = lambda *a, **k: _Run()
    m.set_tags = lambda *a, **k: None
    m.log_params = lambda *a, **k: None
    m.log_metric = lambda *a, **k: None
    m.set_tag = lambda *a, **k: None
    sys.modules["mlflow"] = m


def load_hpo():
    _stub_mlflow()
    if TRAINING not in sys.path:
        sys.path.insert(0, TRAINING)
        sys.path.insert(0, os.path.join(TRAINING, "flows"))
    spec = importlib.util.spec_from_file_location(
        "hpo_under_test", os.path.join(TRAINING, "flows", "hpo.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_search_spaces():
    if TRAINING not in sys.path:
        sys.path.insert(0, TRAINING)
    spec = importlib.util.spec_from_file_location(
        "search_spaces_under_test", os.path.join(TRAINING, "search_spaces.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    """In-memory MLflow registry client: records winner tags + which versions got deleted."""

    def __init__(self):
        self.tags = {}      # (name, version) -> {k: v}
        self.deleted = []   # [(name, version)]

    def set_model_version_tag(self, name, version, key, value):
        self.tags.setdefault((name, str(version)), {})[key] = value

    def delete_model_version(self, name, version):
        self.deleted.append((name, str(version)))


class TrainRecorder:
    """A fake `train_fn`: registers a unique version per call (so each trial 'creates' a version) and
    records call order. `fail_on` trial-indices raise (simulating OOM / convert failure)."""

    def __init__(self, output_name, fail_on=(), produce_no_model=()):
        self.output_name = output_name
        self.fail_on = set(fail_on)
        self.produce_no_model = set(produce_no_model)
        self.calls = []          # the req dict per call, in order
        self._n = 0

    def __call__(self, req):
        idx = self._n
        self._n += 1
        self.calls.append(req)
        if idx in self.fail_on:
            raise RuntimeError(f"simulated training failure on trial {idx}")
        if idx in self.produce_no_model:
            return {"model": None}
        version = str(idx + 1)
        return {"run_id": f"r{version}", "model": {"name": self.output_name, "version": version},
                "metrics": {"train_loss": 0.1}, "params": req}
