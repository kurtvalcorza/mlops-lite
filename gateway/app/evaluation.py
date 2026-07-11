"""Offline evaluation harness + promotion gate + champion-challenger (011, US1/US2/US3).

The platform can register many model versions and promote one to `@serving`, but *promotion was
ungated* — `registry.promote` moved the alias on command with no check that the new version is
actually better (or even non-broken). That is the gap that sank DIMER by omission. 011 adds the
connective tissue:

  - **evaluate()** (US1) — score a registered version on a small held-out benchmark for its modality,
    computing a per-modality *primary metric* (with a known *direction*) and logging it to MLflow
    against that version (version tags + a run metric) with the benchmark's provenance (name + hash),
    so every version carries a comparable, reproducible score (FR-100/FR-101).
  - **gate()** (US2) — before the `@serving` alias moves, compare the candidate's logged metric
    against the incumbent's, honouring direction + like-for-like, and return a GateVerdict
    (pass/warn/blocked). Wired into the single `registry.promote` choke-point (FR-103/FR-104/FR-105).
  - **compare()** (US3) — score the `@serving` champion and a challenger on the same held-out set,
    **sequentially** (one model in VRAM at a time, Principle II), and declare a per-metric winner
    (FR-106). Shadow-replay is deferred to a 013-dependent follow-on.

**Dependency-light by design (Principle III, FR-102).** Like `monitoring.py` (which computes PSI in
pure Python rather than pulling Evidently+pandas+scipy onto the constrained Windows C: drive), the
primary metrics here are implemented in **pure Python** — no sklearn / numpy / jiwer landing in the
gateway image. The two *committed* modalities (LLM task-accuracy, vision top-1 accuracy) need only
string/equality math; the guidance-stub metrics (WER, recall@k, AUC, perplexity) are likewise pure.
Heavier libs (`jiwer`, `sacrebleu`, `scikit-learn`) remain swappable behind this metric interface
(Principle V) if a modality later needs them.
"""
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Callable, Optional

_log = logging.getLogger(__name__)

# mlflow + the registry module are imported lazily inside the functions that touch MLflow, so the
# pure metric/verdict/benchmark logic imports (and unit-tests) with zero third-party dependencies —
# the same dependency-light stance as monitoring.py.

REGISTRY_TASK_TAG = "task"  # mirrors registry.TASK_TAG (009 FR-074); kept local to avoid an eager import
# The configured text-generation LLM (mirrors serving.SERVING_MODEL; read locally to keep this module
# import-light/offline-testable). An untagged @serving version of THIS model is text-generation by
# construction — the eval path falls back to that, exactly as /infer does (see _version_modality).
SERVING_MODEL = os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")

# --- configuration (operator-settable; defaults captured in specs/011-evaluation-gates) ------------

# Regression handling when BOTH candidate and incumbent carry a like-for-like metric.
#   "block" (default) — refuse a candidate that regresses beyond tolerance (hard-gate, FR-104).
#   "warn"            — allow but flag the promotion.
GATE_MODE = os.getenv("EVAL_GATE_MODE", "block").strip().lower()
# Relative regression tolerance vs the incumbent metric (fraction of |incumbent|). A small default
# absorbs benchmark noise without silently promoting a real regression.
GATE_TOLERANCE = float(os.getenv("EVAL_GATE_TOLERANCE", "0.01"))
# What to do when a candidate or incumbent has NO logged metric. Default "warn" keeps the platform
# bootstrappable — every pre-011 version is unevaluated, so blocking on a missing metric would wedge
# promotion. An operator who wants strictness sets this to "block" (FR-104). Never a *silent* pass.
GATE_MISSING_METRIC = os.getenv("EVAL_GATE_MISSING_METRIC", "warn").strip().lower()

# Version-tag keys the harness writes and the gate reads back (a metric lookup, never a model load).
TAG_METRIC = "eval_metric"
TAG_VALUE = "eval_value"
TAG_DIRECTION = "eval_direction"
TAG_MODALITY = "eval_modality"
TAG_BENCHMARK = "eval_benchmark"
TAG_BENCHMARK_HASH = "eval_benchmark_hash"

HIGHER, LOWER = "higher", "lower"  # metric directions


class EvalError(Exception):
    """An evaluation/gate operation failed (no benchmark, no such version, serving unreachable)."""


class EvalGuardError(EvalError):
    """The gateway `/evaluate` guard refused (FR-143): the requested version is not the `@serving` model
    and carries no logged eval metric, so the gateway cannot evaluate it without silently scoring the
    wrong (resident) model. A subclass of EvalError so existing handlers still catch it, but distinct so
    the router can map it to a 409 (a refusal, not an internal eval failure)."""


# --- primary metrics: pure-Python scorers, each tagged with its direction -------------------------

def _norm(s: str) -> str:
    """Loose normalisation for QA-style string match: casefold, collapse whitespace, drop edge
    punctuation. Keeps task-accuracy robust to trivial formatting differences."""
    return " ".join(str(s).strip().lower().split()).strip(".,!?;:\"'`")


def _contains_subsequence(haystack: list, needle: list) -> bool:
    """True if `needle` appears as a contiguous run of tokens in `haystack`."""
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def task_accuracy(preds, refs) -> float:
    """LLM QA primary metric (higher-better): fraction of items whose generated text matches the
    reference answer — exact (normalised) or the reference as a contiguous **token run** (a generative
    answer may wrap the target in a sentence, e.g. "the answer is 7" ⊇ "7"). Token-level on purpose:
    raw substring would count "17" as containing "7", or "blue" as containing "lu", inflating the
    score — and an inflated metric is the one direction the gate can't catch."""
    if not refs:
        raise EvalError("empty benchmark — nothing to score")
    hits = 0
    for p, r in zip(preds, refs):
        np_, nr = _norm(p), _norm(r)
        if nr and (np_ == nr or _contains_subsequence(np_.split(), nr.split())):
            hits += 1
    return hits / len(refs)


def accuracy(preds, refs) -> float:
    """Top-1 accuracy (higher-better): fraction of exact label matches. Vision's primary metric."""
    if not refs:
        raise EvalError("empty benchmark — nothing to score")
    return sum(1 for p, r in zip(preds, refs) if str(p) == str(r)) / len(refs)


def _edit_distance(a: list, b: list) -> int:
    """Levenshtein distance between two token sequences (pure DP, O(len(a)*len(b)) space-optimised)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def wer(hyps, refs) -> float:
    """Word Error Rate (lower-better), pure-Python — total word edits / total reference words. ASR's
    primary metric (guidance stub until 009's ASR serving path is wired; `jiwer` is the swap-in)."""
    edits = words = 0
    for h, r in zip(hyps, refs):
        rt = str(r).split()
        edits += _edit_distance(str(h).split(), rt)
        words += len(rt)
    if words == 0:
        raise EvalError("empty reference — WER undefined")
    return edits / words


def recall_at_k(retrieved, relevant, k: int = 5) -> float:
    """recall@k (higher-better): fraction of queries whose relevant id appears in the top-k retrieved.
    Embeddings' primary metric (guidance stub until 009's embedding serving path is wired)."""
    if not relevant:
        raise EvalError("empty benchmark — nothing to score")
    hits = sum(1 for got, rel in zip(retrieved, relevant) if rel in list(got)[:k])
    return hits / len(relevant)


def auc(scores, labels) -> float:
    """ROC AUC (higher-better) via the rank-sum (Mann-Whitney U) identity — pure Python, no sklearn.
    Tabular's primary metric (guidance stub until 009's tabular serving path is wired)."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        raise EvalError("AUC needs both positive and negative labels")
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):  # average ranks within ties (1-based)
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    rank_pos = sum(ranks[i] for i in range(len(scores)) if labels[i] == 1)
    n_pos, n_neg = len(pos), len(neg)
    return (rank_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def perplexity(nlls, _refs=None) -> float:
    """Perplexity (lower-better): exp(mean per-token negative log-likelihood). The universal LLM
    fallback when no QA answer key exists — the serving path must return per-token logprobs."""
    flat = [x for seq in nlls for x in (seq if isinstance(seq, (list, tuple)) else [seq])]
    if not flat:
        raise EvalError("no token log-likelihoods — perplexity undefined")
    return math.exp(sum(flat) / len(flat))


@dataclass(frozen=True)
class Metric:
    name: str
    direction: str
    score: Callable


# Per-modality default primary metric + direction (the grilled defaults; all operator-configurable).
# Keyed by the registry `task` tag (009 FR-074). LLM + vision + ASR + embeddings all score at
# registration as of 015 (each ships a held-out fixture); tabular has no fine-tune flow → still a stub.
METRICS = {
    "text-generation": Metric("task_accuracy", HIGHER, task_accuracy),  # LLM (committed)
    "image-classification": Metric("accuracy", HIGHER, accuracy),       # vision (committed)
    "asr": Metric("wer", LOWER, wer),                                   # 015 — WER fixture shipped
    "embedding": Metric("recall_at_k", HIGHER, recall_at_k),            # 015 — recall@k fixture shipped
    "tabular": Metric("auc", HIGHER, auc),                              # stub (no fine-tune flow)
}
# Universal LLM fallback (used when a QA answer key is absent) — kept out of METRICS so it is opt-in.
PERPLEXITY = Metric("perplexity", LOWER, perplexity)


def direction_for(metric_name: Optional[str]) -> Optional[str]:
    """The known direction (`higher`/`lower`) of a metric by name, from the registry — or None if the
    name isn't one we define. Lets `read_eval` recover a missing direction tag instead of guessing."""
    if metric_name == PERPLEXITY.name:
        return PERPLEXITY.direction
    for m in METRICS.values():
        if m.name == metric_name:
            return m.direction
    return None


def metric_for(modality: str, name: Optional[str] = None) -> Metric:
    """The primary Metric for a modality (or an explicitly-named one, incl. the perplexity fallback)."""
    if name:
        if name == PERPLEXITY.name:
            return PERPLEXITY
        m = METRICS.get(modality)
        if m and m.name == name:
            return m
        raise EvalError(f"unknown metric {name!r} for modality {modality!r}")
    m = METRICS.get(modality)
    if not m:
        raise EvalError(f"no default metric for modality {modality!r} (configure one)")
    return m


# --- benchmark fixtures (small, held-out, content-hashed for provenance — FR-101) -----------------

BENCHMARKS_DIR = os.getenv(
    "BENCHMARKS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "benchmarks"),
)
DEFAULT_BENCHMARKS = {
    "text-generation": "llm/qa_smoke.jsonl",
    "image-classification": "vision/shapes_smoke.jsonl",
    "embedding": "embedding/recall_smoke.jsonl",  # 015 — recall@k held-out fixture (score-at-registration)
    "asr": "asr/wer_smoke.jsonl",                  # 015 — WER held-out fixture (score-at-registration)
}


@dataclass(frozen=True)
class Benchmark:
    name: str      # provenance identifier (relative path under benchmarks/)
    digest: str    # short sha256 of the file bytes — a score is only meaningful with its benchmark
    rows: list     # the held-out items (list of dicts)


def load_benchmark(modality: str, ref: Optional[str] = None) -> Benchmark:
    """Load a held-out fixture for `modality` (or an explicit `ref` path under benchmarks/), returning
    its rows plus a content hash so the eval is attributable + reproducible (FR-101)."""
    rel = ref or DEFAULT_BENCHMARKS.get(modality)
    if not rel:
        raise EvalError(f"no default benchmark for modality {modality!r} — pass one explicitly")
    path = rel if os.path.isabs(rel) else os.path.join(BENCHMARKS_DIR, rel)
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        raise EvalError(f"cannot read benchmark {rel!r}: {e}") from e
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise EvalError(f"benchmark {rel!r} is empty")
    return Benchmark(name=rel, digest=hashlib.sha256(raw).hexdigest()[:16], rows=rows)


# --- US1: the offline evaluation harness ----------------------------------------------------------

def _version_modality(c, name: str, version: str) -> str:
    """The modality (registry `task` tag) of a registered version — the metric+benchmark key."""
    from mlflow.exceptions import MlflowException

    try:
        mv = c.get_model_version(name, str(version))
    except MlflowException as e:
        raise EvalError(f"no version {name}@{version}: {e}") from e
    task = dict(mv.tags or {}).get(REGISTRY_TASK_TAG)
    if not task:
        # A legacy/untagged version of the configured serving LLM is text-generation by construction:
        # mirror /infer's SERVING_MODEL fallback so the committed LLM eval modality (011) still resolves
        # when the @serving version predates 009 tagging (the served LLM can sit on an untagged version).
        # Any OTHER untagged model genuinely can't be guessed → still an error.
        if name == SERVING_MODEL:
            return "text-generation"
        raise EvalError(f"{name}@{version} has no '{REGISTRY_TASK_TAG}' tag — cannot pick a metric")
    return task


def evaluate(name: str, version: str, benchmark: Optional[str] = None, *,
             metric_name: Optional[str] = None, predict_fn: Optional[Callable] = None,
             client=None, log: bool = True) -> dict:
    """Score `name@version` on a held-out benchmark, compute its modality's primary metric, and (when
    `log`) record it to MLflow against the version — version tags for the gate's metric lookup plus a
    run metric for observability — with the benchmark's provenance (FR-100/FR-101).

    `predict_fn(rows, modality, version) -> list[prediction]` runs the held-out items through the
    **serving path** for that specific version (one model in VRAM); it defaults to the live
    per-modality predictor and is injectable for offline tests. The metric is deterministic given
    (version, benchmark, predictions) → re-running yields the same score (SC-064).
    """
    c = client or _client()
    modality = _version_modality(c, name, version)
    metric = metric_for(modality, metric_name)
    bench = load_benchmark(modality, benchmark)
    predict = predict_fn or _live_predictor(modality)

    preds = predict(bench.rows, modality, str(version))
    refs = [r.get("answer", r.get("label", r.get("text"))) for r in bench.rows]
    value = round(float(metric.score(preds, refs)), 6)

    result = {
        "name": name, "version": str(version), "modality": modality,
        "metric": metric.name, "value": value, "direction": metric.direction,
        "benchmark": bench.name, "benchmark_hash": bench.digest, "n": len(bench.rows),
    }
    if log:
        _log_eval(c, name, version, result)
    return result


def _client():
    """The shared MLflow registry client (deferred so the pure logic imports without mlflow)."""
    from . import registry
    return registry._client()


def _serving_version(c, name: str) -> Optional[str]:
    """The current `@serving` version of `name` (the gate's incumbent / compare's champion), via the
    registry. Deferred import keeps the pure logic mlflow-free and gives tests a single seam to stub."""
    from . import registry
    return registry._serving_version(c, name)


def _log_eval(c, name: str, version: str, result: dict) -> None:
    """Persist an EvalResult to MLflow: version tags (the gate reads these back) + a run metric
    (observability — 'if it isn't tracked, it didn't happen', Principle VI)."""
    from mlflow.exceptions import MlflowException

    tags = {
        TAG_METRIC: result["metric"], TAG_VALUE: str(result["value"]),
        TAG_DIRECTION: result["direction"], TAG_MODALITY: result["modality"],
        TAG_BENCHMARK: result["benchmark"], TAG_BENCHMARK_HASH: result["benchmark_hash"],
    }
    try:
        for k, v in tags.items():
            c.set_model_version_tag(name, str(version), k, v)
        mv = c.get_model_version(name, str(version))
        if mv.run_id:  # log the number on the version's own run, keyed by metric name
            c.log_metric(mv.run_id, f"eval_{result['metric']}", result["value"])
    except MlflowException as e:
        raise EvalError(f"cannot log eval for {name}@{version}: {e}") from e


def read_eval(c, name: str, version: Optional[str]) -> Optional[dict]:
    """The logged EvalResult for a version (from its tags), or None if the version is missing/unevaluated."""
    from mlflow.exceptions import MlflowException

    if version is None:
        return None
    try:
        tags = dict(c.get_model_version(name, str(version)).tags or {})
    except MlflowException:
        return None
    if TAG_METRIC not in tags or TAG_VALUE not in tags:
        return None
    try:
        value = float(tags[TAG_VALUE])
    except (TypeError, ValueError):
        return None
    metric = tags[TAG_METRIC]
    # Direction governs the whole gate, so never *silently* assume higher-better: prefer the logged
    # tag, else recover it from the metric registry by name (every known metric declares its
    # direction — so a WER/perplexity tag written without TAG_DIRECTION still gates lower-better and
    # the gate can't invert). Only an unknown metric with no tag falls back to higher-better, loudly.
    direction = tags.get(TAG_DIRECTION) or direction_for(metric)
    if not direction:
        _log.warning("eval tags for %s@%s carry metric %r with no direction (tag or registry); "
                     "assuming higher-better — set %s to gate it correctly",
                     name, version, metric, TAG_DIRECTION)
        direction = HIGHER
    return {
        "version": str(version), "metric": metric, "value": value,
        "direction": direction, "modality": tags.get(TAG_MODALITY),
        "benchmark": tags.get(TAG_BENCHMARK), "benchmark_hash": tags.get(TAG_BENCHMARK_HASH),
    }


# --- US2: the promotion gate ----------------------------------------------------------------------

def compute_verdict(candidate: Optional[dict], incumbent: Optional[dict], *, mode: str = GATE_MODE,
                    tolerance: float = GATE_TOLERANCE, missing_policy: str = GATE_MISSING_METRIC,
                    override: bool = False, incumbent_present: Optional[bool] = None) -> dict:
    """Pure gate math — decide pass / warn / blocked from a candidate vs incumbent EvalResult.

    Honours each metric's **direction** (higher/lower-better) and compares **like-for-like** (same
    modality + metric); refuses to judge a mismatch (FR-103). No-incumbent → pass; a missing metric on
    either side → the missing-metric policy (never a silent pass, FR-104). A genuine regression beyond
    `tolerance` blocks in hard-gate mode, warns in warn mode or with an explicit `override`.
    """
    def verdict(v, reason, *, flagged=False, delta=None):
        return {
            "verdict": v, "reason": reason, "flagged": flagged, "mode": mode,
            "tolerance": tolerance, "override": bool(override),
            "candidate": _brief(candidate), "incumbent": _brief(incumbent),
            "delta": delta,
        }

    # Distinguish "nothing is @serving" (genuine first promotion → pass) from "an incumbent IS serving
    # but was never evaluated" (read_eval returned None for *both*). The caller (gate) passes
    # incumbent_present from the @serving version's existence; pure callers that hand in an explicit
    # incumbent dict fall back to inferring it from the dict (backward-compatible).
    if incumbent_present is None:
        incumbent_present = incumbent is not None
    if not incumbent_present:
        return verdict("pass", "no incumbent — first promotion")
    # An incumbent IS serving: a missing metric on EITHER side → the missing-metric policy, never a
    # silent pass (FR-104). An unevaluated incumbent must not wave every candidate through the gate.
    if candidate is None or candidate.get("value") is None:
        return (verdict("blocked", "candidate has no logged eval metric", flagged=True)
                if missing_policy == "block" and not override
                else verdict("warn", "candidate has no logged eval metric (missing-metric policy)",
                             flagged=True))
    if incumbent is None or incumbent.get("value") is None:
        return (verdict("blocked", "incumbent has no logged eval metric", flagged=True)
                if missing_policy == "block" and not override
                else verdict("warn", "incumbent has no logged eval metric (missing-metric policy)",
                             flagged=True))
    # like-for-like — a vision metric vs an LLM metric is meaningless (FR-103).
    if (candidate.get("metric") != incumbent.get("metric")
            or candidate.get("modality") != incumbent.get("modality")):
        return (verdict("warn", "metric/modality mismatch — cannot judge (override)", flagged=True)
                if override
                else verdict("blocked", "metric/modality mismatch — refusing to judge", flagged=True))

    # Recover direction the same way read_eval does — never a silent higher-better assumption — so a
    # dict handed straight to this pure function (bypassing read_eval) still gates lower-better right.
    direction = candidate.get("direction") or direction_for(candidate.get("metric")) or HIGHER
    cand, inc = candidate["value"], incumbent["value"]
    delta = round(cand - inc, 6)  # signed: candidate minus incumbent
    margin = tolerance * abs(inc)
    regressed = (cand < inc - margin) if direction == HIGHER else (cand > inc + margin)

    if not regressed:
        return verdict("pass", "candidate within tolerance / improves on incumbent", delta=delta)
    if override:
        return verdict("warn", "regression overridden by operator", flagged=True, delta=delta)
    if mode == "warn":
        return verdict("warn", "regression (warn mode)", flagged=True, delta=delta)
    return verdict("blocked", "candidate regresses beyond tolerance", flagged=True, delta=delta)


def _brief(ev: Optional[dict]) -> Optional[dict]:
    if not ev:
        return None
    return {k: ev.get(k) for k in ("version", "metric", "value", "direction", "modality")}


def gate(name: str, candidate_version: str, *, override: bool = False, mode: str = GATE_MODE,
         tolerance: float = GATE_TOLERANCE, missing_policy: str = GATE_MISSING_METRIC,
         client=None) -> dict:
    """The promotion gate (FR-103/FR-104): fetch the candidate's + the current `@serving` incumbent's
    logged eval metric and return a GateVerdict. A pure metric lookup — it loads no model."""
    c = client or _client()
    incumbent_version = _serving_version(c, name)
    if incumbent_version == str(candidate_version):
        incumbent_version = None  # re-promoting the serving version: nothing to regress against
    candidate = read_eval(c, name, candidate_version)
    incumbent = read_eval(c, name, incumbent_version)
    v = compute_verdict(candidate, incumbent, mode=mode, tolerance=tolerance,
                        missing_policy=missing_policy, override=override,
                        incumbent_present=incumbent_version is not None)
    # Always expose the candidate's version, even when it has no logged metric (its brief is None) —
    # so the UI can offer an override on a missing-metric block (it needs a version to re-promote).
    if v["candidate"] is None:
        v["candidate"] = {"version": str(candidate_version)}
    return v


# --- US3: offline champion-challenger -------------------------------------------------------------

def compare(name: str, challenger_version: str, benchmark: Optional[str] = None, *,
            metric_name: Optional[str] = None, predict_fn: Optional[Callable] = None,
            client=None) -> dict:
    """Declare a winner between the `@serving` champion and a challenger by reading their **logged eval
    metrics** — no model reload (015 FR-142, SC-090). After 015 every fine-tuned version is scored at
    registration, so both versions already carry a comparable metric; `compare` is a pure metric lookup
    (exactly like the gate), so the read and the gate agree by construction. Loading no model trivially
    preserves the VRAM mutex (Principle II) — there is no longer a degenerate "both legs hit the resident
    model" path (the SC-068 mislabel this closes).

    `benchmark`/`metric_name`/`predict_fn` are accepted for signature compatibility but unused: the
    comparison reads each version's logged metric + benchmark provenance, not a re-score.
    """
    c = client or _client()
    champion_version = _serving_version(c, name)
    if champion_version is None:
        raise EvalError(f"{name} has no @serving champion to compare against")

    champ = read_eval(c, name, champion_version)
    chall = read_eval(c, name, str(challenger_version))
    if champ is None or champ.get("value") is None:
        raise EvalError(f"champion {name}@{champion_version} has no logged eval metric — it is scored at "
                        f"registration (015); evaluate/serve it before comparing")
    if chall is None or chall.get("value") is None:
        raise EvalError(f"challenger {name}@{challenger_version} has no logged eval metric — it is scored "
                        f"at registration (015); evaluate/serve it before comparing")
    # like-for-like — comparing a vision metric against an LLM one is meaningless (mirrors the gate).
    if champ["metric"] != chall["metric"] or champ.get("modality") != chall.get("modality"):
        raise EvalError(f"cannot compare {name}@{champion_version} ({champ['metric']}) vs "
                        f"@{challenger_version} ({chall['metric']}) — metric/modality mismatch")

    direction = champ["direction"]
    if champ["value"] == chall["value"]:
        winner = "tie"
    elif (chall["value"] > champ["value"]) == (direction == HIGHER):
        winner = "challenger"
    else:
        winner = "champion"
    # Surface a provenance flag if the two were scored on different benchmark bytes — the values are
    # still both logged, but the operator should know they aren't strictly the same held-out set.
    benchmark_mismatch = (champ.get("benchmark_hash") != chall.get("benchmark_hash"))
    return {
        "name": name, "metric": champ["metric"], "direction": direction,
        "benchmark": champ.get("benchmark"), "benchmark_hash": champ.get("benchmark_hash"),
        "champion": {"version": str(champion_version), "value": champ["value"]},
        "challenger": {"version": str(challenger_version), "value": chall["value"]},
        "winner": winner, "delta": round(chall["value"] - champ["value"], 6),
        "benchmark_mismatch": benchmark_mismatch,
    }


# --- US3 (015): the gateway /evaluate guard (FR-143) ----------------------------------------------

def evaluate_guarded(name: str, version: str, benchmark: Optional[str] = None, *,
                     metric_name: Optional[str] = None, predict_fn: Optional[Callable] = None,
                     client=None) -> dict:
    """Gateway `/evaluate` with the 015 guard (FR-143, contracts/evaluate-guard.md): never silently
    score the resident model for a *different* requested version (the SC-068 mislabel). Three cases:

      - requested version **is** `@serving` → the resident model IS the requested one → score it via the
        serving path (unchanged `evaluate()`), `200` with a freshly-computed EvalResult.
      - requested version **has a logged metric** (scored at registration, 015) → return that logged
        metric, no model reload, `200`.
      - requested version is **not `@serving`** AND has **no logged metric** → raise `EvalGuardError`
        (the router maps it to a clear `409`) — never a wrong-model score.
    """
    c = client or _client()
    serving = _serving_version(c, name)
    if serving is not None and str(version) == str(serving):
        # The resident model is exactly the requested version — scoring it is correct (and re-logs it).
        return evaluate(name, version, benchmark, metric_name=metric_name, predict_fn=predict_fn,
                        client=c)
    logged = read_eval(c, name, str(version))
    if logged is not None and logged.get("value") is not None:
        return {
            "name": name, "version": str(version), "modality": logged.get("modality"),
            "metric": logged["metric"], "value": logged["value"], "direction": logged["direction"],
            "benchmark": logged.get("benchmark"), "benchmark_hash": logged.get("benchmark_hash"),
            "source": "logged",  # read from the registration-time metric (015), not re-scored
        }
    raise EvalGuardError(
        f"{name}@{version} is not the @serving model and has no logged eval metric — promote/serve it "
        f"to evaluate, or it is scored at registration (015)")


# --- live per-modality predictors (the serving path; injected as predict_fn for tests) ------------

def _live_predictor(modality: str) -> Callable:
    """The default predictor for a modality — runs each held-out item through the existing serving
    path (one model in VRAM), so champion-challenger inherits the GPU lease for free."""
    if modality == "text-generation":
        return _predict_llm
    if modality == "image-classification":
        return _predict_vision
    raise EvalError(f"no live serving path for modality {modality!r} yet (guidance stub) — "
                    f"pass predict_fn= to evaluate it")


def _engine_base(engine: str, override_env: str) -> str:
    """The live predictor base URL for an agent engine (023 US1, FR-277/278).

    Pre-023 these read `SERVING_URL`/`BENTO_URL` with the RETIRED per-daemon port defaults
    (:8090/:8092 — dead since 018 folded every engine into the one host agent at :8100), so an
    unconfigured live evaluation dialed endpoints nothing listens on. The default now derives from
    the canonical consolidated topology — `platformlib.topology.agent_url()` + the engine sub-path —
    exactly like `gateway/app/settings.py`. The env override survives (an explicitly configured
    deployment keeps working); only the silent dead default is gone.

    platformlib is imported lazily but is present in every runtime that loads this module: the
    gateway image COPYs it, and every standalone load (training/scoring, HPO) reaches this module
    THROUGH `platformlib.gateway_bridge` — so the import cannot be the thing that breaks
    standalone loading (FR-279)."""
    override = os.getenv(override_env)
    if override:
        return override
    from platformlib.topology import agent_url

    return f"{agent_url()}/engines/{engine}"


def _predict_llm(rows, _modality, _version) -> list:
    """Greedy single-shot generations for each QA prompt via the host agent's llm engine (the GPU
    admission tenant). Scores the model the agent currently serves for text-generation; wiring an
    on-demand load of a *specific* registered version is the on-hardware step for SC-068."""
    import httpx

    url = _engine_base("llm", "SERVING_URL")  # → <agent>/engines/llm/infer (FR-278)
    out = []
    with httpx.Client(timeout=300) as client:
        for r in rows:
            resp = client.post(f"{url}/infer", json={
                "prompt": r["prompt"], "max_tokens": int(r.get("max_tokens", 32)), "temperature": 0.0,
            })
            resp.raise_for_status()
            out.append(resp.json().get("text", ""))
    return out


def _predict_vision(rows, _modality, _version) -> list:
    """Top-1 label for each held-out image via the host agent's vision engine (GPU tenant)."""
    import base64

    import httpx

    url = _engine_base("vision", "BENTO_URL")  # → <agent>/engines/vision/classify (FR-278)
    out = []
    with httpx.Client(timeout=120) as client:
        for r in rows:
            raw = base64.b64decode(r["image_b64"], validate=True)
            resp = client.post(f"{url}/classify", files={"image": ("image.png", raw, "image/png")})
            resp.raise_for_status()
            data = resp.json()
            preds = data.get("predictions") or data.get("labels") or []
            out.append(preds[0]["label"] if preds and isinstance(preds[0], dict) else
                       (preds[0] if preds else data.get("label", "")))
    return out
