"""Shared offline harness for the 011 evaluation tests.

Loads `gateway/app/evaluation.py` as an isolated module (like test_tracing_resilience loads
tracing.py) so the pure metric / verdict / benchmark logic runs anywhere — no live stack, no mlflow
install. A tiny `mlflow.exceptions` stub satisfies the lazy imports inside the harness, and a fake
MLflow client records the tags + run metrics the harness writes, so US1 logging and the US2 gate are
testable deterministically and offline.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_PATH = os.path.join(REPO, "gateway", "app", "evaluation.py")


def _stub_mlflow():
    """Register a minimal `mlflow.exceptions` so the harness's lazy `from mlflow.exceptions import
    MlflowException` resolves without the real package."""
    if "mlflow.exceptions" in sys.modules:
        return
    mlflow = sys.modules.get("mlflow") or types.ModuleType("mlflow")
    exc = types.ModuleType("mlflow.exceptions")

    class MlflowException(Exception):
        pass

    exc.MlflowException = MlflowException
    mlflow.exceptions = exc
    sys.modules["mlflow"] = mlflow
    sys.modules["mlflow.exceptions"] = exc


def load_evaluation():
    """A fresh, isolated copy of the harness module."""
    _stub_mlflow()
    spec = importlib.util.spec_from_file_location("evaluation_under_test", EVAL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeMV:
    """Stand-in for an MLflow ModelVersion (just the fields the harness reads)."""

    def __init__(self, tags=None, run_id="run-1"):
        self.tags = dict(tags or {})
        self.run_id = run_id


class FakeClient:
    """In-memory MLflow registry client: enough surface for evaluate()/gate()/read_eval()."""

    def __init__(self, versions=None):
        self.versions = dict(versions or {})  # (name, version) -> FakeMV
        self.logged_metrics = []              # (run_id, key, value)

    def get_model_version(self, name, version):
        key = (name, str(version))
        if key not in self.versions:
            from mlflow.exceptions import MlflowException
            raise MlflowException(f"no model version {name}@{version}")
        return self.versions[key]

    def set_model_version_tag(self, name, version, key, value):
        self.versions[(name, str(version))].tags[key] = value

    def log_metric(self, run_id, key, value):
        self.logged_metrics.append((run_id, key, value))
