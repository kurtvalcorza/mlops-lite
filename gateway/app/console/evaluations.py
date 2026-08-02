"""Evaluations, gates, and drift (027 T737/T738 — data-model §9).

Three rules, each of which the obvious implementation gets wrong:

**Metrics are modality-native and are not coerced** (FR-399). A WER, an accuracy, a recall@k and a
perplexity are not comparable, and projecting them onto a single "score" column would produce a
leaderboard that ranks an ASR model against a classifier. Each metric therefore travels with its own
name and its own direction, and nothing here computes a cross-modality aggregate.

**A gate failure carries its evidence** (FR-400 / SC-191): the rule that produced it, the observed
value, and the incumbent it was compared against — all reachable without leaving the view. A verdict
without its evidence sends the operator to the tracking UI to reconstruct why, which is the round
trip this surface exists to remove. An override travels with its recorded reason (FR-401), because
an override with no reason is indistinguishable from a gate that was never enforced.

**Drift thresholds ship inline** (FR-405). The 0.10 / 0.25 PSI convention is configuration, and an
interface that hard-codes it silently disagrees with the backend the day someone tunes it.
"""

#: `EvaluationResult.gate.outcome`, data-model §9.
GATE_OUTCOMES = ("passed", "failed", "warning", "not-evaluated", "incomplete")

#: The platform's own gate verdicts → the console vocabulary. Mapped rather than renamed at source:
#: `evaluation.compute_verdict` is the gate, and changing its words to suit a screen would be the
#: interface dictating terms to the thing it is describing.
_VERDICT_TO_OUTCOME = {"pass": "passed", "blocked": "failed", "warn": "warning"}

#: `DriftReport.features[].state`. Named states rather than a raw number so the surface does not
#: have to re-derive the banding — and so the banding has exactly one definition.
DRIFT_STATES = ("stable", "warning", "significant")


def evaluation_result(*, name, version, evaluation=None, verdict=None, modality=None,
                      source_job_id=None, created_at=None):
    """One `EvaluationResult` (FR-398/400/401).

    `evaluation` is `read_eval`'s record (or `None` for an unevaluated version); `verdict` is
    `compute_verdict`'s output (or `None` when no gate has been run).
    """
    metrics = []
    if evaluation and evaluation.get("value") is not None:
        metrics.append({
            "name": evaluation.get("metric"),
            "value": evaluation.get("value"),
            # Direction travels with every metric. Without it, "0.31" cannot be read as good or bad,
            # and a surface that assumed higher-better would rank every WER backwards.
            "direction": ("higher-better" if evaluation.get("direction") == "higher"
                          else "lower-better"),
        })

    return {
        "id": f"{name}:{version}",
        "modelName": name,
        "version": str(version),
        "modality": modality or (evaluation or {}).get("modality"),
        "datasetRef": (evaluation or {}).get("dataset"),
        "benchmarkName": (evaluation or {}).get("benchmark"),
        "benchmarkDigest": (evaluation or {}).get("benchmark_hash"),
        # Never coerced into a common metric (FR-399): the list carries whatever this modality
        # actually measures, under its own name.
        "metrics": metrics,
        "gate": gate_view(evaluation=evaluation, verdict=verdict),
        "sourceJobId": source_job_id,
        "createdAt": created_at,
    }


def gate_view(*, evaluation=None, verdict=None):
    """The gate block, with the evidence a failure needs (SC-191)."""
    if verdict is None:
        # No gate has run. `not-evaluated` when there is nothing to gate; `incomplete` when there is
        # a metric but no verdict — those are different situations and collapsing them would hide
        # which one an operator is looking at.
        return {"outcome": "not-evaluated" if not evaluation else "incomplete",
                "failedRule": None, "observedValue": None, "comparedAgainst": None,
                "override": {"applied": False, "reason": None}}

    outcome = _VERDICT_TO_OUTCOME.get(verdict.get("verdict"), "incomplete")
    candidate = verdict.get("candidate") or {}
    incumbent = verdict.get("incumbent") or {}

    failed_rule = None
    if outcome in ("failed", "warning") and candidate.get("metric"):
        # The rule as the gate actually applied it, reconstructed from the gate's own parameters
        # rather than restated: the tolerance and direction here are the ones that produced this
        # verdict, so the operator is reading the reason, not an approximation of it.
        higher_better = candidate.get("direction") != "lower"
        failed_rule = {
            "metric": candidate.get("metric"),
            "operator": "gte" if higher_better else "lte",
            "threshold": _threshold(incumbent.get("value"), verdict.get("tolerance"), higher_better),
            "scope": candidate.get("modality"),
        }

    return {
        "outcome": outcome,
        "reason": verdict.get("reason"),
        "mode": verdict.get("mode"),
        "tolerance": verdict.get("tolerance"),
        "failedRule": failed_rule,
        "observedValue": candidate.get("value"),
        "comparedAgainst": ({"version": incumbent.get("version"), "value": incumbent.get("value")}
                            if incumbent.get("value") is not None else None),
        "delta": verdict.get("delta"),
        # An override with no reason is indistinguishable from a gate that was never enforced
        # (FR-401), so the reason travels with the flag rather than being logged elsewhere.
        "override": {"applied": bool(verdict.get("override")),
                     "reason": verdict.get("override_reason")},
    }


def _threshold(incumbent_value, tolerance, higher_better):
    """The value the candidate had to beat, in the metric's own units."""
    if incumbent_value is None or tolerance is None:
        return None
    margin = tolerance * abs(incumbent_value)
    return round(incumbent_value - margin if higher_better else incumbent_value + margin, 6)


# -- drift ------------------------------------------------------------------------------------------

def drift_report(report, *, warning=0.10, significant=0.25):
    """One `DriftReport` with its `thresholds` **inline** (FR-404/405).

    Shipping the thresholds means the interface never restates the 0.10 / 0.25 convention, so tuning
    it in configuration cannot leave the console quietly disagreeing with the backend about what
    counts as drift.
    """
    features = []
    for feature in report.get("features") or []:
        statistic = feature.get("psi", feature.get("statistic"))
        features.append({
            "name": feature.get("name"),
            "statistic": statistic,
            "state": _drift_state(statistic, warning, significant),
        })

    statistics = [f["statistic"] for f in features if f["statistic"] is not None]
    return {
        "modelName": report.get("model_name") or report.get("cur_name"),
        "endpointId": report.get("endpoint_id"),
        "referenceWindow": {"from": report.get("ref_from"), "to": report.get("ref_to")},
        "currentWindow": {"from": report.get("cur_from"), "to": report.get("cur_to")},
        "featureCount": len(features),
        # `None`, not `0.0`, when nothing was measurable — a max of zero reads as "no drift", which
        # is a claim an empty feature list cannot support.
        "maxStatistic": max(statistics) if statistics else None,
        "features": features,
        "thresholds": {"warning": warning, "significant": significant, "configurable": True},
        "calculatedAt": report.get("created_at"),
    }


def _drift_state(statistic, warning, significant):
    if statistic is None:
        return None
    if statistic >= significant:
        return "significant"
    return "warning" if statistic >= warning else "stable"


#: FR-406. A **static property of the surface**, not data — which is why it lives in the projection
#: rather than being computed per report: every drift number the console shows is subject to all
#: four of these, and attaching them to individual reports would imply that some report somewhere is
#: exempt.
DRIFT_LIMITATIONS = [
    "This detects distributional change between two windows. It does not prove model degradation.",
    "It does not establish causality — a shifted input distribution may be entirely benign.",
    "The result depends on the chosen baseline window; a different baseline gives a different answer.",
    "The result depends on binning; PSI is computed over reference deciles.",
]


# -- comparison ---------------------------------------------------------------------------------------

#: FR-403. The comparison workspace keeps these **separated** rather than rolling them into a single
#: "better/worse" verdict: a challenger that wins on quality and loses on latency is a trade-off an
#: operator has to make, and a combined score would make it for them silently.
COMPARISON_DIMENSIONS = ("quality", "latency", "resources", "artifacts", "datasets", "policy")


def comparison(*, challenger, champion, challenger_eval=None, champion_eval=None, verdict=None):
    """A champion-challenger comparison across the six dimensions, kept apart."""
    def quality(evaluation):
        if not evaluation:
            return None
        # Same shape as `EvaluationResult.metrics[]` — `name`, not `metric`. One metric shape across
        # both surfaces means a component can render either without knowing which it was handed.
        return {"name": evaluation.get("metric"), "value": evaluation.get("value"),
                "direction": ("higher-better" if evaluation.get("direction") == "higher"
                              else "lower-better")}

    return {
        "challenger": {"name": challenger.get("name"), "version": challenger.get("version")},
        "champion": ({"name": champion.get("name"), "version": champion.get("version")}
                     if champion else None),
        "quality": {"challenger": quality(challenger_eval), "champion": quality(champion_eval)},
        # The remaining dimensions are reported as `null` rather than fabricated. This platform does
        # not record per-version latency, resource, or policy history yet, and inventing a column of
        # zeros would be worse than an empty one: a zero invites comparison.
        "latency": {"challenger": None, "champion": None},
        "resources": {"challenger": None, "champion": None},
        "artifacts": {"challenger": challenger.get("source"),
                      "champion": (champion or {}).get("source")},
        "datasets": {"challenger": (challenger_eval or {}).get("benchmark"),
                     "champion": (champion_eval or {}).get("benchmark")},
        "policy": {"gate": gate_view(evaluation=challenger_eval, verdict=verdict)},
    }
