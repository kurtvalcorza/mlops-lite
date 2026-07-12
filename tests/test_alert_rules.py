"""023 US7 (T538, FR-322/323) — the local Prometheus alert rules, statically validated.

promtool isn't in the dev environment, so the offline validation is structural: the YAML parses,
the required alerts exist, every rule has an expr + for + fixed-vocabulary severity + an operator
summary + a runbook reference that RESOLVES to a real anchor in monitoring/README.md, and every
metric an expression references is one the platform actually exports (agent describe()-table +
gateway source scan) — a renamed metric can't leave a silently dead alert.
"""
import os
import re
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent.metrics import REGISTRY  # noqa: E402

RULES = os.path.join(REPO, "infra", "prometheus", "rules", "mlops-lite.yml")

REQUIRED_ALERTS = {
    "WedgedEngine", "ProlongedGpuHold", "ActivationDegraded", "MigrationFailed",
    "RepeatedSchedulerFailures", "AgentSaturated", "LowDiskSpace",
    "ServingStoreUnavailable", "AgentScrapeDown",
}

SEVERITIES = {"warning", "critical"}


@pytest.fixture(scope="module")
def rules():
    doc = yaml.safe_load(open(RULES, encoding="utf-8"))
    out = [r for g in doc["groups"] for r in g["rules"]]
    assert out, "rules file parsed but contains no rules"
    return out


def test_required_alerts_exist(rules):
    names = {r["alert"] for r in rules}
    assert REQUIRED_ALERTS <= names, f"missing alerts: {sorted(REQUIRED_ALERTS - names)}"


def test_every_rule_is_complete_and_operator_actionable(rules):
    readme = open(os.path.join(REPO, "monitoring", "README.md"), encoding="utf-8").read().lower()
    for r in rules:
        name = r["alert"]
        assert r.get("expr"), f"{name}: no expr"
        assert r.get("for"), f"{name}: no persistence window (transient flapping unfiltered)"
        assert r.get("labels", {}).get("severity") in SEVERITIES, f"{name}: bad severity"
        ann = r.get("annotations", {})
        assert ann.get("summary"), f"{name}: no operator summary (FR-323)"
        runbook = ann.get("runbook", "")
        assert runbook.startswith("monitoring/README.md#"), f"{name}: no runbook link (FR-323)"
        anchor = runbook.split("#", 1)[1]
        assert f"alert-{name.lower()}" == anchor and anchor in readme, \
            f"{name}: runbook anchor #{anchor} not found in monitoring/README.md"


def test_expressions_reference_only_exported_metrics(rules):
    agent_metrics = set(REGISTRY._help)
    gw_src = ""
    for root, _dirs, files in os.walk(os.path.join(REPO, "gateway", "app")):
        for f in files:
            if f.endswith(".py"):
                gw_src += open(os.path.join(root, f), encoding="utf-8").read()
    known_other = {"up"}  # Prometheus's own scrape-liveness series
    for r in rules:
        for metric in re.findall(r"\b([a-z][a-z0-9_]{3,})\b(?:\{|\s|\[|=|$)", r["expr"]):
            if metric in ("increase", "rate", "and", "or", "unless", "for", "on", "by"):
                continue
            ok = (metric in agent_metrics or f'"{metric}"' in gw_src or metric in known_other)
            assert ok, f"{r['alert']}: expression references unexported metric {metric!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
