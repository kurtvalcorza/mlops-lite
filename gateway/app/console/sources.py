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

#: Process start, for the admin surface's uptime. Captured at import rather than read from
#: `/proc`, so the number describes THIS gateway process — which is what an operator asking
#: "how long has it been up" means, not how long the container has existed.
_STARTED_AT = time.time()

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


def evaluation_for(name: str, version: str):
    """The logged evaluation record for one version, or `None` when it was never scored."""
    from .. import evaluation, registry
    return evaluation.read_eval(registry._client(), name, str(version))


def gate_verdict(name: str, version: str):
    """The gate's own verdict for a candidate against the current champion.

    Computed by `evaluation.compute_verdict` — the same function the promote path uses. A second
    implementation here would eventually disagree with the gate that actually blocks promotions,
    and the disagreement would surface as a console that says "passed" over a refused promote.
    """
    from .. import evaluation, registry
    client = registry._client()
    champion = registry._serving_version(client, name)
    return evaluation.compute_verdict(
        evaluation.read_eval(client, name, str(version)),
        evaluation.read_eval(client, name, champion),
        incumbent_present=champion is not None)


def prediction_rows(limit: int = 100, modality: str = None, model_name: str = None) -> list:
    """Paged prediction records joined to their label state.

    From the gateway's own table, never reconstructed from traces (FR-407): traces are sampled, and
    a prediction list built from them would silently omit the untraced ones — "the request I am
    looking for is missing" being the worst possible failure for an audit surface.
    """
    sql = ("SELECT p.prediction_id, p.modality, p.model_name, p.version, p.served_at, "
           "p.payload_ref, l.label, l.submitted_at AS labeled_at "
           "FROM predictions p LEFT JOIN labels l USING (prediction_id) ")
    where, params = [], []
    if modality:
        where.append("p.modality = %s")
        params.append(modality)
    if model_name:
        where.append("p.model_name = %s")
        params.append(model_name)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY p.served_at DESC LIMIT %s"
    with _conn().cursor() as cur:
        cur.execute(sql, (*params, int(limit)))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def prediction_row(prediction_id: str):
    """One prediction by id — an index hit, not a scan of the platform's largest table."""
    with _conn().cursor() as cur:
        cur.execute("SELECT p.prediction_id, p.modality, p.model_name, p.version, p.served_at, "
                    "p.payload_ref, l.label FROM predictions p LEFT JOIN labels l USING "
                    "(prediction_id) WHERE p.prediction_id = %s", (prediction_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c.name for c in cur.description], row))


def capture_rows_for(modality: str = None, limit: int = 200) -> list:
    """The capture index joined to labels — the input to both the capture list and the queue."""
    sql = ("SELECT c.prediction_id, c.input_ref, c.captured_at, c.modality, "
           "p.model_name, l.label FROM capture_index c "
           "LEFT JOIN predictions p USING (prediction_id) "
           "LEFT JOIN labels l USING (prediction_id) ")
    params = []
    if modality:
        sql += "WHERE c.modality = %s "
        params.append(modality)
    sql += "ORDER BY c.captured_at DESC LIMIT %s"
    with _conn().cursor() as cur:
        cur.execute(sql, (*params, int(limit)))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def payload_bytes(payload_ref: str) -> bytes:
    """The stored payload for an explicit reveal. Read only when asked."""
    from platformlib import store

    bucket, _, key = str(payload_ref)[len("s3://"):].partition("/")
    return store.s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()


def traces(limit: int = 50) -> list:
    """Recent traces from the tracking server (research R6 — the already-pinned client)."""
    from .. import registry
    client = registry._client()
    search = getattr(client, "search_traces", None)
    if search is None:
        # The pinned client does not expose trace search. `[]` is wrong here and an exception is
        # right: the caller records `tracking` degraded and the console says "unknown" rather than
        # "no traces", which would be a claim this deployment cannot make.
        raise RuntimeError("this tracking client exposes no trace search")
    return list(search(max_results=limit))


def trace(trace_id: str):
    from .. import registry
    client = registry._client()
    getter = getattr(client, "get_trace", None)
    if getter is None:
        raise RuntimeError("this tracking client exposes no trace lookup")
    return getter(trace_id)


def serving_pointers() -> list:
    """Every task's desired serving target, from the registry alias."""
    from .. import registry
    out = []
    for model in registry.list_models():
        if not model.get("serving_version"):
            continue
        versions = registry.list_versions(model["name"])
        current = next((v for v in versions if str(v["version"]) == str(model["serving_version"])),
                       {})
        out.append({"modelName": model["name"], "version": str(model["serving_version"]),
                    "alias": "serving", "modality": (current.get("tags") or {}).get("task"),
                    "engine": (current.get("tags") or {}).get("serving_engine")})
    return out


def dataset_detail(name: str, version: str):
    from .. import datasets as datasets_mod
    return datasets_mod.get_dataset(name, version)


def alert_rules() -> list:
    """The 023 US7 Prometheus rule files, parsed for rule NAMES and expressions.

    Read from the shipped rule files rather than from a rules API: this deployment has Prometheus
    but no Alertmanager, so there is no evaluation endpoint to ask for live state. Every rule
    therefore comes back `unknown` — which is the honest answer and is exactly why `unknown` is a
    first-class member of the state vocabulary rather than a fallback to `inactive`.
    """
    import glob
    import os

    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    rules = []
    for path in sorted(glob.glob(os.path.join(repo, "monitoring", "**", "*rules*.y*ml"),
                                 recursive=True)):
        with open(path, encoding="utf-8") as fh:
            current = None
            for raw in fh:
                line = raw.strip()
                if line.startswith("- alert:"):
                    current = {"name": line.split(":", 1)[1].strip(), "state": "unknown",
                               "annotations": {}, "labels": {}}
                    rules.append(current)
                elif current is not None and line.startswith("expr:"):
                    current["expr"] = line.split(":", 1)[1].strip()
                elif current is not None and line.startswith("severity:"):
                    current["labels"]["severity"] = line.split(":", 1)[1].strip()
                elif current is not None and line.startswith("runbook_url:"):
                    current["annotations"]["runbook_url"] = line.split(":", 1)[1].strip()
    return rules


def dashboards() -> list:
    """The configured dashboards, with embeddability resolved here rather than in the browser."""
    import os

    from . import observability
    from .console_helpers import frame_policy_allows

    base = os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    embeddable, reason = frame_policy_allows()

    return [observability.dashboard_embed(
        id="platform", title="Platform overview",
        external_url=f"{base}/dashboards", embeddable=embeddable, reason=reason)]


def bucket_summaries() -> list:
    """Buckets with object counts and sizes. One listing per bucket, bounded by the pager."""
    import os

    from platformlib import store

    client = store.s3_client()
    names = [b for b in (os.getenv("GARAGE_BUCKETS", "datasets,models,results,inputs").split(","))
             if b.strip()]
    out = []
    for bucket in names:
        bucket = bucket.strip()
        try:
            keys = store.list_keys(client, bucket, "")
            out.append({"bucket": bucket, "objectCount": len(keys), "sizeBytes": None,
                        "reachable": True})
        except Exception:  # noqa: BLE001 — one unreachable bucket is not the whole page
            # `None` counts, not zero: an unreadable bucket has not been measured, and "0 objects"
            # would read as an empty bucket.
            out.append({"bucket": bucket, "objectCount": None, "sizeBytes": None,
                        "reachable": False})
    return out


def migration_ledger() -> dict:
    """The checksummed migration ledger (023 US4). **Read-only** — never triggers an apply."""
    from platformlib import store

    with _conn().cursor() as cur:
        cur.execute("SELECT id, applied_at, checksum FROM schema_migrations ORDER BY id")
        rows = cur.fetchall()
    return {
        "schemaVersion": str(store.SCHEMA_VERSION),
        "migrations": [{"id": r[0], "appliedAt": r[1].isoformat() if r[1] else None,
                        "checksumState": "ok" if r[2] else "unapplied"} for r in rows],
        "reachable": True,
    }


def integrations(*, agent_reachable: bool) -> list:
    import os

    from ..settings import AGENT_URL
    from . import observability

    return [
        observability.integration("host agent", endpoint=AGENT_URL, reachable=agent_reachable),
        observability.integration("tracking", endpoint=os.getenv("MLFLOW_TRACKING_URI"),
                                  reachable=None),
        observability.integration("object store", endpoint=os.getenv("S3_ENDPOINT"),
                                  reachable=None),
        observability.integration("database", endpoint=os.getenv("GATEWAY_DB_URL"),
                                  reachable=None),
    ]


def system_info() -> dict:
    import os
    import platform

    return {
        "platformVersion": os.getenv("PLATFORM_VERSION"),
        "constitutionVersion": os.getenv("CONSTITUTION_VERSION", "1.6.1"),
        "host": platform.node(),
        "uptimeSeconds": int(time.time() - _STARTED_AT),
    }

