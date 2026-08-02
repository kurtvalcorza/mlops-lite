"""Source readers for the console projections (027 T701–T703, reused by T721/T726 onward).

Each function reads **one** backend and returns a list of plain dicts. None of them catch anything:
a failing source must raise so `Projection.source()` records it as degraded and hands the composer a
`None`. A reader that swallowed its own failure and returned `[]` would erase exactly the distinction
the envelope exists to preserve — the console cannot tell "no jobs" from "the job table did not
answer" if the reader already decided for it.

Bounded by default. Every reader here can be called on a poll cadence by an interface that is open
all day, so an unbounded read is not an option (contract cross-cutting rule 3).
"""
import time

#: Default caps. Chosen to be a screenful of context rather than a full history: these feed summary
#: surfaces, and the per-area views page properly against their own routes.
JOB_LIMIT = 200
MODEL_LIMIT = 100
DRIFT_LIMIT = 20


def _conn():
    from .. import broker
    return broker.conn()


def jobs(limit: int = JOB_LIMIT) -> list:
    """Agent job records, newest first, from the shared job table."""
    from platformlib import store
    return store.list_jobs(_conn())[:limit]


def active_job_count() -> int:
    from platformlib import store
    return store.count_active_jobs(_conn())


def drift_reports(limit: int = DRIFT_LIMIT) -> list:
    """Recent drift reports, normalized to carry `max_psi` under one name.

    `compute_drift` records the score under whichever key its own version used; normalizing here
    means the attention rule and the drift area agree about which number they are ranking.
    """
    from .. import monitoring
    out = []
    for report in monitoring.latest_reports(limit):
        score = report.get("max_psi")
        if score is None:
            scores = [f.get("psi") for f in (report.get("features") or []) if f.get("psi") is not None]
            score = max(scores) if scores else None
        out.append({**report, "max_psi": score})
    return out


def model_versions(limit: int = MODEL_LIMIT) -> list:
    """Every registry version across every model, flattened.

    Flattened on purpose: the attention rules and the catalog both ask version-level questions, and
    a nested shape would make each caller re-walk the same tree.
    """
    from .. import registry
    out = []
    for model in registry.list_models():
        for version in registry.list_versions(model["name"]):
            out.append({**version, "serving_version": model.get("serving_version")})
            if len(out) >= limit:
                return out
    return out


def unlabeled_count() -> int:
    """Captured predictions with no label yet — the review backlog.

    Counted in SQL rather than by listing objects: this is polled, and a listing that grows with the
    capture index is the 018 regression in a different costume.
    """
    with _conn().cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_index c "
                    "LEFT JOIN labels l USING (prediction_id) WHERE l.prediction_id IS NULL")
        return cur.fetchone()[0]


def datasets() -> list:
    from .. import datasets as datasets_mod
    return datasets_mod.list_datasets()


def predictions(limit: int = 50, prediction_id: str = None) -> list:
    """Recent prediction records, or one by id.

    The by-id path exists so search can resolve a pasted id without scanning: an id lookup is an
    index hit, and a substring scan of this table is the one read that can genuinely hurt.
    """
    sql = ("SELECT prediction_id, modality, model_name, version, served_at "
           "FROM predictions ")
    params: tuple = ()
    if prediction_id is not None:
        sql += "WHERE prediction_id = %s "
        params = (prediction_id,)
    sql += "ORDER BY served_at DESC LIMIT %s"
    with _conn().cursor() as cur:
        cur.execute(sql, params + (limit,))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def heartbeat_age_s(last_seen: float = None) -> float:
    """Seconds since the agent last answered. `None` last_seen means it has never answered."""
    return float("inf") if last_seen is None else max(0.0, time.time() - last_seen)


def broker_jobs(limit: int = JOB_LIMIT) -> list:
    """The gateway's own job lane (026). Distinct from `jobs()`, which is the agent's table.

    Two lanes, two state machines, two id spaces — which is exactly why the console joins them
    rather than picking one. `id` is renamed to `job_id` here so the join keys on one field name
    instead of teaching the joiner about both.
    """
    from platformlib import store
    return [{**row, "job_id": row.get("id")} for row in store.list_broker_jobs(_conn(), limit=limit)]


def tracking_runs(limit: int = JOB_LIMIT) -> list:
    """Tracking runs across every experiment — **net-new**; only `GET /runs/{id}` existed.

    `job_id` is lifted out of the run's tags when the launcher recorded it, which is what lets a run
    join to the job that produced it rather than sitting in its own list.
    """
    from .. import registry
    client = registry._client()
    experiments = client.search_experiments()
    out = []
    for experiment in experiments:
        for run in client.search_runs([experiment.experiment_id], max_results=limit):
            tags = dict(run.data.tags or {})
            out.append({
                "run_id": run.info.run_id,
                "name": tags.get("mlflow.runName") or run.info.run_id,
                "experiment_id": experiment.experiment_id,
                "experiment_name": experiment.name,
                "status": run.info.status,
                "start_time": run.info.start_time,
                "end_time": run.info.end_time,
                "job_id": tags.get("job_id"),
                "metrics": dict(run.data.metrics or {}),
                "params": dict(run.data.params or {}),
            })
            if len(out) >= limit:
                return out
    return out


def experiments() -> list:
    from .. import registry
    client = registry._client()
    return [{"experiment_id": e.experiment_id, "name": e.name,
             "lifecycle_stage": e.lifecycle_stage}
            for e in client.search_experiments()]


def artifact_present(uri: str) -> bool:
    """Whether the object a registry version points at actually exists (FR-384).

    An **existence check**, not an inference from the URI. A registry row is a pointer, and a
    pointer is exactly the kind of thing that outlives what it points at — inferring presence from
    it is how a console shows a download that 404s.

    Raises on an unreachable object store, so the caller records the source degraded and the field
    stays `None`. `None` is not `False`: an unchecked artifact is not a missing one, and reporting
    it as missing would send an operator hunting for a file that is probably there.
    """
    from platformlib import store

    if not uri or not str(uri).startswith("s3://"):
        # A non-S3 source (a local path from an offline run) cannot be checked from here. Unknown,
        # not absent.
        raise ValueError(f"not an object-store URI: {uri!r}")
    bucket, _, key = str(uri)[len("s3://"):].partition("/")
    client = store.s3_client()
    try:
        client.head_object(Bucket=bucket, Key=key.rstrip("/"))
        return True
    except Exception as e:  # noqa: BLE001
        # A 404 is an answer; anything else is an unreachable store and must propagate.
        code = getattr(getattr(e, "response", None), "get", lambda _k, _d=None: None)("Error", {})
        status = (code or {}).get("Code")
        if str(status) in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def study(study_id: str) -> dict:
    """One HPO study record from the trainer.

    Read through the trainer's own route rather than reconstructed from tracking runs: the trainer
    is the only thing that knows which runs belonged to which study, and rebuilding that from tags
    would produce a second, quietly-divergent answer.
    """
    import httpx

    from ..settings import TRAINER_URL, agent_headers

    with httpx.Client(headers=agent_headers(), timeout=10.0) as client:
        response = client.get(f"{TRAINER_URL}/study/{study_id}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()
