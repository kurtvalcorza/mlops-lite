"""Optuna HPO study runner (012, US1/US2/US3) — wraps the existing fine-tune path.

A **study** is one HPO campaign: it samples N hyperparameter sets from a per-modality search space
(`search_spaces.py`), runs each as a **real `finetune_flow` training run** (US1, FR-111 — reuse, don't
fork the trainer), scores each trained candidate with **011's evaluation metric** (US2, FR-114 — the
objective), and registers the **best** trial as a promotable MLflow version (US3, FR-116).

Non-negotiable constraints baked in here:
  - **Strictly sequential on the one GPU** (`n_jobs=1`) — every trial is a one-model-in-VRAM lease
    tenant, so trials cannot overlap (Principle II, FR-112). Total wall-clock ≈ `n_trials × per-train`.
  - **Server-less Optuna** — `create_study` runs in-process (like the ephemeral Prefect already in the
    flow); default in-memory storage, no daemon, no Ray (Principle III, FR-118).
  - **Failed trial** → recorded pruned, **no surviving version**, GPU freed, study continues (FR-113).
  - **Full trials by default** — no pruning (training loss is an imperfect proxy for 011's eval
    metric); `MedianPruner` is opt-in via `pruner=` (FR-113).

The two heavy seams are **injected** so the orchestration (sequencing, parent/child runs, best-trial
selection, winner registration, loser cleanup) is unit-testable with real Optuna and zero GPU:
  - `train_fn(req) -> result`  — default runs `finetune_flow` in a fresh subprocess (CUDA isolation,
    same as the trainer daemon); raising means a failed trial.
  - `eval_fn(name, version, modality) -> EvalResult` — default calls **011's eval harness**; the trial
    objective is its scalar `value`.
"""
import json
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))           # training/flows
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # training/

_log = logging.getLogger(__name__)

DEFAULT_N_TRIALS = int(os.getenv("HPO_N_TRIALS", "15"))  # small — each trial is a full sequential train
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
HPO_EXPERIMENT = os.getenv("HPO_EXPERIMENT", "hpo")
LEASE_TENANT = "training"  # the GPU-lease identity a study runs under (same as the trainer daemon)

# modality -> the registry `task` tag 011 keys its metric/direction off of.
MODALITY_TASK = {
    "llm": "text-generation", "vision": "image-classification",
    "embeddings": "embedding", "asr": "asr",
}
# Fallback optimize direction per modality, kept in sync with 011's METRICS, used only if 011's
# evaluation module can't be imported to resolve it authoritatively (see optimize_direction).
_FALLBACK_HIGHER = {"llm": True, "vision": True, "asr": False, "embeddings": True}


class HpoError(Exception):
    """A study could not be set up or run (bad modality, no objective, etc.)."""


def optimize_direction(modality: str) -> str:
    """Optuna study direction ("maximize"/"minimize") for `modality`, resolved from **011's** metric
    direction (the single definition of "good", FR-114). Falls back to the known per-modality direction
    only if 011's evaluation module isn't importable — and **warns loudly** when it does, because a
    silently-wrong direction would optimize a study toward *worse* models (e.g. maximizing WER)."""
    try:
        ev = _load_evaluation()
        metric = ev.metric_for(MODALITY_TASK[modality])
        return "maximize" if metric.direction == ev.HIGHER else "minimize"
    except Exception as e:
        direction = "maximize" if _FALLBACK_HIGHER.get(modality, True) else "minimize"
        _log.warning("could not resolve the optimize direction for modality %r from 011's eval "
                     "registry (%s); falling back to %s — verify this matches the metric's direction",
                     modality, e, direction)
        return direction


def _load_evaluation():
    """Import 011's eval harness (gateway/app/evaluation.py) for metric direction + the default
    objective. Deferred + path-injected so HPO's pure logic imports without the gateway tree."""
    gw = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "gateway")
    if gw not in sys.path:
        sys.path.insert(0, gw)
    from app import evaluation
    return evaluation


# --- the default seams (live: subprocess train + 011 eval) ----------------------------------------

def _default_train(req: dict) -> dict:
    """Run one fine-tune as a fresh subprocess (`run_flow.py`) — same CUDA-isolation discipline as the
    trainer daemon, so each trial gets a clean GPU context and a crash can't poison the study process.
    Returns the flow result ({run_id, model:{name,version}, metrics, params}); raises on failure."""
    import subprocess
    import tempfile

    from run_flow import read_result

    run_flow = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_flow.py")
    with tempfile.TemporaryDirectory(prefix="hpo-trial-") as tmp:
        req_path, res_path = os.path.join(tmp, "req.json"), os.path.join(tmp, "res.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(req, f)
        # Popen + record the child as the lease's VRAM owner (matching the trainer daemon's _worker),
        # so the single GPU lease's liveness self-heal fires if a trial process dies/hangs — closing
        # the asymmetry where a hung trial would otherwise strand the lease (review).
        proc = subprocess.Popen([sys.executable, run_flow, req_path, res_path], env=os.environ.copy())
        _set_trial_vram_owner(proc.pid)
        proc.wait()
        rec = read_result(res_path, proc.returncode)
    if rec.get("status") != "completed":
        raise HpoError(rec.get("error", "trial training failed"))
    return {"run_id": rec.get("mlflow_run_id"), "model": rec.get("model"),
            "metrics": rec.get("metrics"), "params": rec.get("params")}


def _set_trial_vram_owner(pid: int) -> None:
    """Record a trial's training subprocess as the GPU lease's VRAM owner (best-effort) so the lease
    self-heals on its death — same liveness discipline as the trainer daemon's `_worker`. A no-op if
    the stdlib-only lease isn't importable (e.g. off the WSL host, or in tests)."""
    try:
        serving = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "serving")
        if serving not in sys.path:
            sys.path.insert(0, serving)
        import gpu_lease
        gpu_lease.set_vram_owner(LEASE_TENANT, pid)
    except Exception:
        pass


def _default_eval(name: str, version: str, modality: str) -> dict:
    """Read the trial's **own registered version's logged eval metric** as the objective (012 FR-114,
    015 FR-141). Since 015 every fine-tune scores its model in-process at registration (the trial's
    `finetune_flow` already logged the metric on the version), so the objective is a pure metric lookup —
    no serving-daemon call, no `host.docker.internal` hostname the native trainer can't resolve (finding
    #4 closed, SC-089). Each trial trains a distinct model, so each logs a distinct metric → the study
    optimizes toward the genuinely best candidate (no degenerate shared-resident-model score, SC-068)."""
    ev = _load_evaluation()
    c = ev._client()
    result = ev.read_eval(c, name, str(version))
    if result is None or result.get("value") is None:
        # The trial trained + registered but carries no logged metric — score-at-registration must have
        # failed (its warn path, T290). A trial with no objective is a FAILED trial, not a worst score.
        raise HpoError(f"trial version {name}@{version} has no logged eval metric — "
                       f"score-at-registration (015) did not log one; treating the trial as failed")
    return result


# --- the study runner -----------------------------------------------------------------------------

def run_study(study_req: dict, *, train_fn=None, eval_fn=None, n_trials: int = DEFAULT_N_TRIALS,
              timeout=None, overrides=None, pruner=None, sampler=None, client=None) -> dict:
    """Run an Optuna study over `study_req` and return a summary (study id, best trial + params +
    metric + registered version, trial count). Sequential (`n_jobs=1`); only the best trial's version
    survives — every other trial's version is deleted so the registry isn't flooded (FR-116)."""
    import optuna

    if n_trials is None:  # never let an explicit None fall through to an unbounded study
        n_trials = DEFAULT_N_TRIALS
    modality = (study_req.get("modality") or "llm").lower()
    output_name = study_req["output_name"]
    train = train_fn or _default_train
    score = eval_fn or _default_eval
    direction = optimize_direction(modality)
    study_id = uuid.uuid4().hex[:12]

    # base trial request = the study request minus HPO-only knobs (the sampler fills the search knobs).
    base_req = {k: v for k, v in study_req.items()
                if k not in ("n_trials", "timeout", "overrides", "pruner")}
    created = []   # (name, version) every trial registered — all but the winner get cleaned up
    trials_log = []

    import mlflow
    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment(HPO_EXPERIMENT)

    with mlflow.start_run(run_name=f"hpo-{output_name}") as parent:
        mlflow.set_tags({"hpo_study": study_id, "modality": modality,
                         "n_trials": str(n_trials), "direction": direction})

        def objective(trial):
            import search_spaces
            params = search_spaces.sample(modality, trial, overrides)
            req = {**base_req, **params, "modality": modality}
            with mlflow.start_run(nested=True):
                mlflow.set_tags({"hpo_study": study_id, "hpo_trial": str(trial.number)})
                mlflow.log_params(params)
                try:
                    result = train(req)
                except Exception as e:  # OOM / convert fail / spawn error → failed trial, no version
                    mlflow.set_tag("trial_status", "failed")
                    trials_log.append({"trial": trial.number, "params": params, "status": "failed",
                                       "error": str(e)})
                    raise optuna.TrialPruned(f"training failed: {e}")
                model = (result or {}).get("model") or {}
                name, version = model.get("name"), model.get("version")
                if not version:
                    mlflow.set_tag("trial_status", "failed")
                    trials_log.append({"trial": trial.number, "params": params, "status": "failed",
                                       "error": "training produced no registered version"})
                    raise optuna.TrialPruned("training produced no model version")
                created.append((name, version))
                try:
                    ev = score(name, version, modality)  # 011 eval — the objective scalar
                except Exception as e:  # a broken eval is a FAILED trial, not a worst-valid score (FR-115)
                    mlflow.set_tag("trial_status", "eval_failed")
                    trials_log.append({"trial": trial.number, "params": params, "version": version,
                                       "status": "eval_failed", "error": str(e)})
                    raise optuna.TrialPruned(f"eval failed: {e}")
                value = float(ev["value"])
                mlflow.log_metric(f"eval_{ev.get('metric', 'objective')}", value)
                mlflow.set_tags({"trial_status": "completed", "trial_version": str(version)})
                trial.set_user_attr("version", str(version))
                trial.set_user_attr("name", name)
                trial.set_user_attr("eval", ev)
                trials_log.append({"trial": trial.number, "params": params, "version": str(version),
                                   "status": "completed", "value": value, "metric": ev.get("metric")})
                return value

        study = optuna.create_study(direction=direction, sampler=sampler, pruner=pruner)
        # n_jobs=1 is the Principle-II invariant: trials never overlap on the single GPU.
        study.optimize(objective, n_trials=n_trials, timeout=timeout, n_jobs=1)

        best = _best_trial(study)
        summary = {
            "study_id": study_id, "parent_run_id": parent.info.run_id, "modality": modality,
            "direction": direction, "n_trials": n_trials, "completed": _count_completed(study),
            "trials": trials_log, "best": None,
        }
        if best is not None:
            winner, cleanup_failed = _register_winner(study_id, best, created, client=client)
            summary["best"] = winner
            mlflow.set_tags({"best_value": str(best.value), "best_version": winner["version"]})
        else:  # every trial failed — nothing to register; clean up any stragglers
            cleanup_failed = _cleanup_versions(created, keep=None, client=client)
        # Surface any versions that couldn't be deleted, so a silent leftover isn't invisible to the API.
        summary["cleanup_failed"] = cleanup_failed
        return summary


def _best_trial(study):
    """The study's best COMPLETE trial, or None if every trial failed/pruned (study.best_trial raises
    when there is no completed trial)."""
    try:
        return study.best_trial
    except (ValueError, RuntimeError):
        return None


def _count_completed(study) -> int:
    import optuna
    return sum(1 for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE)


def _register_winner(study_id, best, created, *, client=None) -> dict:
    """Tag the best trial's already-registered version as the HPO winner (study id + winning params +
    winning metric) so it flows into 011's gated promotion (FR-116), and delete every OTHER version this
    study registered (only the winner survives — no registry flood, FR-116)."""
    c = client or _client()
    name, version = best.user_attrs["name"], best.user_attrs["version"]
    ev = best.user_attrs.get("eval", {})
    tags = {
        "hpo_study": study_id,
        "hpo_best_params": json.dumps(best.params, sort_keys=True),
        "hpo_best_metric": ev.get("metric", ""),
        "hpo_best_value": repr_value(best.value),
    }
    for k, v in tags.items():
        c.set_model_version_tag(name, str(version), k, v)
    cleanup_failed = _cleanup_versions(created, keep=(name, str(version)), client=c)
    return ({"name": name, "version": str(version), "value": best.value,
             "metric": ev.get("metric"), "params": best.params}, cleanup_failed)


def _cleanup_versions(created, *, keep, client=None) -> list:
    """Delete the versions this study registered except `keep` (the winner) — the losing/failed trials
    leave **no** surviving version (FR-116). Only ever deletes versions THIS study created. Returns the
    list of versions that could NOT be deleted (best-effort cleanup, but surfaced — not swallowed)."""
    if not created:
        return []
    c = client or _client()
    failed = []
    for name, version in created:
        if keep is not None and (name, str(version)) == (keep[0], str(keep[1])):
            continue
        try:
            c.delete_model_version(name, str(version))
        except Exception as e:  # a leftover trial version isn't worth failing the study — but record it
            failed.append({"name": name, "version": str(version), "error": str(e)})
    return failed


def repr_value(v) -> str:
    return "" if v is None else str(v)


def _client():
    from mlflow.tracking import MlflowClient
    return MlflowClient(tracking_uri=MLFLOW_URI)
