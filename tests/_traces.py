"""Helper: query the MLflow REST API for inference traces (006 tests).

Uses the MLflow tracking REST API directly (urllib) so the tests need no `mlflow` package on the
host. The trace search returns `request_metadata` carrying `mlflow.traceInputs` / `mlflow.traceOutputs`
(the captured prompt/output), which is enough to verify a request produced a trace and what it stored.
"""
import json
import os
import time
import urllib.request

MLFLOW = f"http://127.0.0.1:{os.getenv('MLFLOW_PORT', '5500')}"
EXPERIMENT = os.getenv("MLFLOW_TRACING_EXPERIMENT", "mlops-lite-inference")


def experiment_id():
    url = f"{MLFLOW}/api/2.0/mlflow/experiments/get-by-name?experiment_name={EXPERIMENT}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)["experiment"]["experiment_id"]
    except Exception:
        return None


def _search(exp_id, n=25):
    url = f"{MLFLOW}/api/2.0/mlflow/traces?experiment_ids={exp_id}&max_results={n}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r).get("traces", [])


def _meta(trace, key):
    for m in trace.get("request_metadata", []):
        if m["key"] == key:
            return m["value"]
    return None


def trace_inputs(trace):
    return _meta(trace, "mlflow.traceInputs")


def trace_outputs(trace):
    return _meta(trace, "mlflow.traceOutputs")


def wait_for_trace(prompt_substr, timeout=25):
    """Poll the experiment until a trace whose captured inputs contain prompt_substr appears.

    Returns the trace dict (status/execution_time_ms/request_metadata/...) or None on timeout —
    accounts for the fire-and-forget export landing a beat after the HTTP response.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # Poll for the experiment INSIDE the timeout: on a fresh MLflow the fire-and-forget exporter
            # may not have created `mlops-lite-inference` yet when /infer returns (006 Codex review).
            exp = experiment_id()
            if exp is not None:
                for t in _search(exp):
                    if prompt_substr in (trace_inputs(t) or ""):
                        return t
        except Exception:
            pass
        time.sleep(1)
    return None
