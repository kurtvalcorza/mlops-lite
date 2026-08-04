"""HPO study projection (027 T727 — FR-396/397).

The one rule that shapes this module: **a study is a set of recorded executions, not a running
search**. There is no persistent optimizer service on this platform — `POST /studies` runs N
trainings sequentially and returns; nothing keeps thinking afterwards. A view that spoke in the
present tense ("exploring", "next trial", "converging") would invite an operator to wait for
something nobody scheduled, so every field here is past tense and the payload carries `completed`
rather than any notion of progress toward a target.

Parameter importance is computed as a plain rank correlation rather than by importing an optimizer's
own importance machinery. That is deliberate: the alternative pulls a search library into the
gateway to explain results the gateway already has, and the correlation is the honest strength of
the claim these trial counts can support anyway.
"""


def trial_view(study) -> dict:
    """`{trials, history, importance, best, axes}` from a trainer study record."""
    trials = [_trial(t) for t in (study.get("trials") or [])]
    scored = [t for t in trials if t["value"] is not None]

    return {
        "study_id": study.get("study_id"),
        # Past tense throughout. `status` is the trainer's own word for whether the sequence of
        # trainings finished, not a claim about an optimizer's state.
        "status": study.get("status"),
        "completed": len(scored),
        "recorded": len(trials),
        "metric": study.get("metric") or (study.get("best") or {}).get("metric"),
        "direction": study.get("direction"),
        "best": study.get("best"),
        "trials": trials,
        # The objective as it was recorded, trial by trial. Not smoothed and not extrapolated: a
        # fitted curve over eight points would be a prediction the data cannot support.
        "history": [{"number": t["number"], "value": t["value"]} for t in scored],
        "axes": _axes(trials),
        "importance": importance(trials),
    }


def _trial(trial) -> dict:
    return {
        "number": trial.get("number"),
        "value": trial.get("value"),
        "state": trial.get("state"),
        "params": dict(trial.get("params") or {}),
        "version": trial.get("version"),
        # A trial that produced no model is FAILED, not scored worst. Scoring it worst would let a
        # crash masquerade as a bad hyperparameter choice.
        "failed": trial.get("state") in ("FAIL", "failed", "PRUNED") or trial.get("value") is None,
    }


def _axes(trials) -> list:
    """The parameter names, in a stable order, for the parallel-coordinates view."""
    names = []
    for trial in trials:
        for key in trial["params"]:
            if key not in names:
                names.append(key)
    return sorted(names)


def importance(trials) -> dict:
    """Spearman rank correlation between each numeric parameter and the objective.

    Rank rather than Pearson because hyperparameters are routinely explored on a log scale, where a
    linear correlation understates a real relationship. Returned per parameter with the count it was
    computed from, so the interface can show how much weight the number deserves — an importance
    from four trials and one from four hundred are not the same claim, and printing them
    identically would be the misleading part.
    """
    scored = [t for t in trials if t["value"] is not None]
    if len(scored) < 3:
        # Below this a correlation is noise. Returned empty rather than as small numbers, because a
        # number on screen reads as a finding no matter how it is captioned.
        return {}

    values = [t["value"] for t in scored]
    out = {}
    for name in _axes(scored):
        column = [t["params"].get(name) for t in scored]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in column):
            continue
        rho = _spearman(column, values)
        if rho is not None:
            out[name] = {"correlation": round(rho, 3), "trials": len(scored)}
    return out


def _spearman(xs, ys):
    ranked_x, ranked_y = _ranks(xs), _ranks(ys)
    n = len(xs)
    mean_x, mean_y = sum(ranked_x) / n, sum(ranked_y) / n
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(ranked_x, ranked_y))
    denominator = (sum((a - mean_x) ** 2 for a in ranked_x)
                   * sum((b - mean_y) ** 2 for b in ranked_y)) ** 0.5
    # A constant column has no rank variance and therefore no correlation to report — `None` rather
    # than 0.0, which would read as "explored and found irrelevant".
    return None if denominator == 0 else numerator / denominator


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0  # ties share the mean rank
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks
