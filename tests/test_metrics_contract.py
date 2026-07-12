"""023 US7 (T537, FR-321) — the metrics contract: required operation families, bounded labels.

Pins (a) every required operation family exists in the owning module's registry/describe table,
and (b) NO metric uses a high-cardinality label key (model names, job ids, request ids, paths):
label VALUES must come from fixed vocabularies, so a busy month can't explode the local
Prometheus into millions of series.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent.metrics import REGISTRY  # noqa: E402

#: Label KEYS a bounded-cardinality contract permits. Anything else — especially model/job/request
#: identifiers — is a forbidden series-explosion vector (FR-321).
ALLOWED_LABEL_KEYS = {"method", "reason", "status", "engine", "state", "result", "mode",
                      "outcome", "op", "kind", "route", "modality", "verdict",
                      # fixed two-value vocabulary (drift|quality) on the retrain trigger counters
                      "signal",
                      # GRANDFATHERED (013 quality gauges, pre-023): grows with registry versions —
                      # slow on a single-operator platform, and renaming would break the shipped
                      # Grafana dashboard. New metrics must NOT add identifier-shaped labels.
                      "model_version"}

#: The agent-side operation families the contract requires (describe()d in hostagent/metrics.py).
AGENT_REQUIRED = {
    "hostagent_gpu_free_gb", "hostagent_slot_held", "hostagent_engine_state",
    "hostagent_jobs_active", "hostagent_wedged",                       # admission/engines/jobs
    "hostagent_requests_total", "hostagent_requests_rejected_total",   # transport
    "hostagent_request_seconds_sum", "hostagent_request_seconds_count",
    "hostagent_client_disconnects_total",
    "hostagent_reload_outcomes_total",                                 # swap/reload
    "hostagent_disk_free_gb",                                          # FR-322 low-disk signal
}

#: Gateway-side families, asserted by scanning the source for the metric constructors — the
#: modules construct them at import and several need mlflow/DB to import, so the source is the
#: honest offline authority for WHAT is exported.
GATEWAY_REQUIRED = {
    "gateway_requests_total": "main.py",                                # requests
    "gateway_registry_ops_total": "routers/models.py",                  # registry ops
    "gateway_policy_checks_total": "scheduler.py",                      # scheduler actions
    "gateway_policy_retrains_total": "scheduler.py",
    "mlops_migrations_db_version": "main.py",                           # database operations
    "mlops_migrations_outcomes_total": "main.py",
    "gateway_activation_outcomes_total": "activation.py",               # activation saga
    "gateway_activation_reconcile_ms": "activation.py",
    "gateway_quality_store_dropped_total": "quality.py",                # store write outcomes
    "mlops_serving_up": "platform_metrics.py",                          # durable-store/agent reach
    "mlops_trainer_up": "platform_metrics.py",
}


def test_agent_registry_describes_every_required_family():
    described = set(REGISTRY._help)
    missing = AGENT_REQUIRED - described
    assert not missing, f"agent metrics missing required families: {sorted(missing)}"


def test_agent_labels_are_bounded_vocabulary():
    REGISTRY.inc("hostagent_requests_total", labels={"method": "GET"})  # seed a labeled series
    with REGISTRY._lock:
        keys = {k for table in (REGISTRY._gauges, REGISTRY._counters)
                for (_name, labels) in table for (k, _v) in labels}
    assert keys <= ALLOWED_LABEL_KEYS, f"forbidden label keys: {sorted(keys - ALLOWED_LABEL_KEYS)}"


@pytest.mark.parametrize("metric,owner", sorted(GATEWAY_REQUIRED.items()))
def test_gateway_module_owns_its_required_family(metric, owner):
    src = open(os.path.join(REPO, "gateway", "app", owner), encoding="utf-8").read()
    assert f'"{metric}"' in src, f"{owner} no longer exports {metric}"


def test_gateway_metric_labels_are_bounded():
    """Every Counter/Gauge label list in gateway/app uses allowed keys only."""
    pat = re.compile(r"(?:Counter|Gauge)\(\s*\"[a-z_]+\"\s*,\s*\"[^\"]*\"\s*,\s*\[([^\]]*)\]")
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(REPO, "gateway", "app")):
        for f in files:
            if not f.endswith(".py"):
                continue
            src = open(os.path.join(root, f), encoding="utf-8").read()
            for m in pat.finditer(src):
                for key in re.findall(r"\"([a-z_]+)\"", m.group(1)):
                    if key not in ALLOWED_LABEL_KEYS:
                        offenders.append(f"{f}: label {key!r}")
    assert not offenders, f"high-cardinality label keys in gateway metrics: {offenders}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
