"""Console read routes (027 T698, T714 — contracts/console-read-api.md).

Read-only projections. They add no persisted entity and require no migration (research R7).

Every route returns the envelope from `gateway/app/console/__init__.py`, so the console can render
data age per source and tell "unknown" apart from "zero". The routes are grouped here rather than
scattered across the existing routers because they are a distinct *surface* — the console's read
model — and mixing them into the lifecycle routers would blur which routes are the platform's API
and which exist to draw a screen.
"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import console, runtime
from ..console import (
    catalog,
    endpoints,
    evaluations,
    observability,
    overview,
    predictions,
    sources,
    studies,
)
from ..console import datasets as datasets_mod
from ..console import jobs as jobs_mod
from ..console import traces as traces_mod

router = APIRouter()


# -- platform health and capabilities (T698) ---------------------------------------------------------

@router.get("/console/health")
async def console_health():
    """`PlatformHealth` — the seven services, the aggregate, and the resolved deployment mode.

    **`overall` and `mode` are different questions** and were briefly conflated here during
    implementation. `overall` is *how well is this platform working* (healthy / degraded / critical
    / unknown, FR-369). `mode` is *what kind of deployment is this* (offline / live / hardware,
    FR-429) — a fixture-backed console and a GPU-backed one are both legitimately `healthy`, and an
    operator needs to know which one they are looking at before trusting any number on the screen.

    Both are resolved from **reachability**, never from a configured string (research R14). A
    deployment that declared itself `hardware` while its agent was down would be describing its
    intention rather than its state.
    """
    projection = console.Projection()

    agent = await runtime.health()
    agent_data = agent["data"]
    agent_reachable = agent_data is not None
    if agent_reachable:
        projection.mark_observed(runtime.AGENT)
    else:
        projection.mark_degraded(runtime.AGENT)

    store_ok = projection.source("database", _store_reachable, default=False)
    registry_ok = projection.source("tracking", _registry_reachable, default=False)
    objectstore_ok = projection.source("objectstore", _objectstore_reachable, default=False)
    metrics_ok = _metrics_reachable()

    # A GPU is not a service the platform can lose independently of the agent — the agent is what
    # measures it. `unknown` when the agent is down, never `critical`: we cannot see the GPU, which
    # is not the same as the GPU being broken.
    gpu_free = (agent_data or {}).get("gpu_free_gb") if agent_reachable else None
    gpu_state = "unknown" if not agent_reachable else (
        "healthy" if gpu_free is not None else "degraded")

    services = [
        # `gateway` is trivially up: this response IS the gateway answering. Listed anyway, because
        # a health panel that silently omits the component it runs inside teaches the reader that
        # the list is not the whole list.
        _service("gateway", "healthy", required=True, detail="answering this request"),
        _service("database", "healthy" if store_ok else "critical", required=True),
        _service("tracking", "healthy" if registry_ok else "degraded", required=False),
        _service("objectstore", "healthy" if objectstore_ok else "degraded", required=False),
        # Agent loss is `degraded`, NOT `critical`: the CPU modalities (embeddings, tabular) still
        # serve, and asserting otherwise overstates the outage (data-model §2/§11).
        _service("agent", "healthy" if agent_reachable else "degraded", required=False),
        _service("metrics", "healthy" if metrics_ok else "degraded", required=False),
        _service("gpu", gpu_state, required=False),
    ]

    return projection.envelope({
        "overall": _overall(services),
        "mode": _mode(agent_data, store_ok or registry_ok),
        "services": services,
        # Kept as a map alongside the list: the list is the contract (data-model §2), the map is
        # what a one-line summary needs, and deriving one from the other in three components would
        # be three chances to derive it differently.
        "reachable": {"gateway": True, "database": store_ok, "tracking": registry_ok,
                      "objectstore": objectstore_ok, "agent": agent_reachable,
                      "metrics": metrics_ok},
        "gpu_free_gb": gpu_free,
        "jobs_active": (agent_data or {}).get("jobs_active") if agent_reachable else None,
        "wedged": (agent_data or {}).get("wedged") if agent_reachable else None,
        "observedAt": console.utcnow(),
    })


def _service(name, state, *, required, detail=None):
    return {"service": name, "state": state, "required": required, "detail": detail,
            "observedAt": console.utcnow()}


def _overall(services):
    """FR-369/370. `critical` is reserved for a **required** service being down — concretely the
    gateway or the database, without which training and inference cannot operate safely.

    `unknown` is not produced here: this response is the gateway answering, so at least one service
    is genuinely observed. A console that cannot reach the gateway at all renders `unknown` from the
    absence of a response, which is the only honest place for that value to come from.
    """
    if any(s["required"] and s["state"] == "critical" for s in services):
        return "critical"
    if any(s["state"] in ("degraded", "critical", "unknown") for s in services):
        return "degraded"
    return "healthy"


def _mode(agent_data, any_backend):
    """FR-429: `offline` / `live` / `hardware`, resolved from what is actually reachable.

    `hardware` requires the agent to be reporting a GPU reading, not merely to be up: an agent
    running on a host with no usable device is a `live` deployment, and badging it `hardware` would
    tell an operator that GPU numbers on screen mean something when they do not.
    """
    if agent_data is not None and agent_data.get("gpu_free_gb") is not None:
        return "hardware"
    return "live" if (agent_data is not None or any_backend) else "offline"


def _objectstore_reachable() -> bool:
    from platformlib import store
    store.s3_client().list_buckets()
    return True


def _metrics_reachable() -> bool:
    """The gateway's own Prometheus registry, which is in-process and therefore always readable.

    Reported rather than assumed so the seven-service matrix has an entry for it. A separate metrics
    *store* is not part of this deployment; when one is added, this becomes a real probe and nothing
    downstream changes.
    """
    return True


@router.get("/console/capabilities")
async def capabilities():
    """What this deployment can do (FR-433).

    This is what lets the interface **omit** an unsupported control rather than render one that
    fails. A console that shows every control and lets the backend reject the unsupported ones
    teaches an operator that the interface is unreliable.
    """
    agent = await runtime.health()
    broker = _broker_enabled()
    return console.envelope({
        "runtime_reads": agent["data"] is not None,
        "broker": broker,
        "co_residency": _co_residency_enabled(),
        "jobs_lane": broker,
        # Gated in 026 on a native-Linux host with a passing sandbox spike plus a constitution
        # amendment. Reported as unavailable so the console omits the control entirely rather than
        # offering a submit button that always fails.
        "tenant_jobs": False,
        "sessions": False,
    })


def _store_reachable() -> bool:
    from .. import broker as broker_mod
    broker_mod.conn()
    return True


def _registry_reachable() -> bool:
    from .. import registry
    registry.list_models()
    return True


# -- attention, activity, search (T701/T702/T703) -------------------------------------------------

@router.get("/console/attention")
async def attention():
    """Severity-ranked `AttentionItem[]` covering all nine kinds (FR-373).

    Composed from whatever answered. A source that fails contributes **nothing** and is named in
    `degraded`, so the panel can render "unknown" — an attention panel that says "nothing needs
    attention" while half the platform is unreadable is the console's most dangerous falsehood, and
    it is the exact failure this route is shaped to avoid.
    """
    projection = console.Projection()

    agent_body = await runtime.health()
    agent_data = agent_body["data"]
    if agent_data is None:
        projection.mark_degraded(runtime.AGENT)
    else:
        projection.mark_observed(runtime.AGENT)

    admission_body = await runtime.admission(64)
    admission_data = admission_body["data"]
    if admission_data is None and agent_data is not None:
        projection.mark_degraded(runtime.AGENT)

    # Concurrently: four sequential timeouts would make a fully-degraded console take twenty
    # seconds to report that it knows nothing.
    jobs, unlabeled, drift, versions = await asyncio.gather(
        projection.read("store", sources.jobs),
        projection.read("store", sources.unlabeled_count),
        projection.read("objectstore", sources.drift_reports),
        projection.read("registry", sources.model_versions))

    items = overview.attention_items(
        now=console.utcnow(), agent=agent_data, admission=admission_data, jobs=jobs, drift=drift,
        versions=versions, unlabeled=unlabeled,
        # An unreachable agent has no heartbeat to age; the `degraded` entry already says so, and
        # emitting a stale-heartbeat item on top would report one outage as two.
        heartbeat_age_s=None)
    return projection.envelope(items)


@router.get("/console/summary")
async def summary():
    """The eight Overview cards (FR-371), each `null` when its source did not answer.

    Computed here rather than in the client so the null-is-not-zero rule has one home. A card that
    fell back to `0` in the browser would be indistinguishable from a genuine zero, and "0 running
    jobs" during an agent outage is the exact falsehood SC-195 exists to catch.
    """
    projection = console.Projection()

    agent_body = await runtime.health()
    if agent_body["data"] is None:
        projection.mark_degraded(runtime.AGENT)
    else:
        projection.mark_observed(runtime.AGENT)
    device_body = await runtime.devices()
    admission_body = await runtime.admission(64)

    jobs, unlabeled, drift, versions = await asyncio.gather(
        projection.read("store", sources.jobs),
        projection.read("store", sources.unlabeled_count),
        projection.read("objectstore", sources.drift_reports),
        projection.read("registry", sources.model_versions))

    return projection.envelope(overview.summary_cards(
        agent=agent_body["data"], devices=(device_body["data"] or {}).get("devices"),
        admission=admission_body["data"], jobs=jobs, versions=versions, unlabeled=unlabeled,
        drift=drift))


@router.get("/console/activity")
async def activity(limit: int = Query(50, ge=1, le=200)):
    """The normalized lifecycle timeline (FR-363).

    021 navigated by loop stage; 027 navigates by area and keeps the loop **here**, as a
    visualization. The stages still describe how work moves through the platform — they just no
    longer decide where an operator clicks.
    """
    projection = console.Projection()
    jobs, versions, drift = await asyncio.gather(
        projection.read("store", sources.jobs),
        projection.read("registry", sources.model_versions),
        projection.read("objectstore", sources.drift_reports))
    return projection.envelope(
        overview.activity_events(jobs=jobs, versions=versions, drift=drift, limit=limit))


@router.get("/console/search")
async def search(q: str = Query(..., min_length=1, max_length=200),
                 limit: int = Query(20, ge=1, le=100)):
    """Composed resolver across models, runs, datasets, jobs, endpoints, predictions (FR-368).

    The prediction table is queried **only** for a query that looks like an identifier. It is the
    largest table on the platform, and letting a two-character name fragment scan it would make the
    search box the most expensive control in the console.
    """
    projection = console.Projection()
    models, jobs, datasets, preds = await asyncio.gather(
        projection.read("registry", sources.model_versions),
        projection.read("store", sources.jobs),
        projection.read("objectstore", sources.datasets),
        (projection.read("store", lambda: sources.predictions(prediction_id=q))
         if overview.looks_like_id(q) else _nothing()))

    return projection.envelope(overview.search_results(
        q, models=models, jobs=jobs, datasets=datasets, predictions=preds, limit=limit))


# -- catalog and compatibility (T721/T722) ----------------------------------------------------------

@router.get("/console/catalog")
async def console_catalog(modality: str = None, evaluation_state: str = None,
                          deployed: bool = None, verify_artifacts: bool = False,
                          limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    """The joined catalog across registry, object store, tracking, and evaluation (FR-383/384).

    `verify_artifacts` is opt-in. An existence check per row is a round trip per row, and the
    catalog is a list view — doing it unconditionally would make the page cost grow with the
    registry. Unverified rows carry `artifactPresent: null`, which the console renders as
    "unchecked" rather than as present.
    """
    projection = console.Projection()
    versions = await projection.read("registry", sources.model_versions)
    if versions is None:
        return projection.envelope(None)

    rows = []
    for version in versions:
        present = None
        if verify_artifacts:
            try:
                present = await projection.read("objectstore",
                                                lambda v=version: sources.artifact_present(v.get("source")))
            except Exception:  # noqa: BLE001 — a check that cannot run leaves the field unknown
                present = None
        rows.append(catalog.platform_model(version, artifact_present=present))

    if modality:
        rows = [r for r in rows if r["modality"] == modality]
    if evaluation_state:
        rows = [r for r in rows if r["evaluationState"] == evaluation_state]
    if deployed is not None:
        rows = [r for r in rows if bool(r["deploymentIds"]) is deployed]

    return projection.envelope({"models": rows[offset:offset + limit], "total": len(rows),
                                "offset": offset, "limit": limit})


@router.get("/console/catalog/{name}/{version}")
async def console_catalog_detail(name: str, version: str, verify_artifact: bool = True):
    """One catalog row plus lineage (FR-386/390). The artifact IS checked here — a detail view is
    one row, so the round trip the list view avoids is affordable and the answer is what the
    operator opened the page for."""
    projection = console.Projection()
    versions = await projection.read("registry", sources.model_versions)
    if versions is None:
        return projection.envelope(None)

    match = [v for v in versions if v.get("name") == name and str(v.get("version")) == version]
    if not match:
        return projection.envelope(None)

    present = None
    if verify_artifact:
        present = await projection.read("objectstore",
                                        lambda: sources.artifact_present(match[0].get("source")))
    return projection.envelope(catalog.platform_model(match[0], artifact_present=present))


@router.get("/console/catalog/{name}/{version}/compatibility")
async def console_compatibility(name: str, version: str):
    """`RuntimeCompatibility` against **live** topology (FR-387/388/389).

    A statement about *now*, so it is computed per request and not cached beyond the device
    snapshot's own TTL — a cached "eligible" outlives the free VRAM that made it true.
    """
    projection = console.Projection()

    admission_body = await runtime.admission(1)
    engines_body = await runtime.engines()
    if admission_body["data"] is None:
        projection.mark_degraded(runtime.AGENT)
    else:
        projection.mark_observed(runtime.AGENT)

    versions = await projection.read("registry", sources.model_versions)
    match = [v for v in versions or [] if v.get("name") == name and str(v.get("version")) == version]
    row = catalog.platform_model(match[0]) if match else None

    engines = (engines_body["data"] or {}).get("engines") if engines_body["data"] else None
    tags = dict((match[0] if match else {}).get("tags") or {})
    required_engine = tags.get(catalog.ENGINE_TAG)
    estimated = _estimated_vram_gb(engines, required_engine)

    return projection.envelope(catalog.compatibility(
        estimated_gb=estimated,
        admission=admission_body["data"],
        engines=engines,
        required_engine=required_engine,
        artifact_available=(row or {}).get("artifactPresent"),
        base_resolvable=((row or {}).get("lineage") or {}).get("baseResolvable", True)))


def _estimated_vram_gb(engines, required_engine):
    """The estimate the **agent** publishes for the engine that would serve this model.

    Sourced from the agent rather than computed here on purpose: the agent's estimate is the number
    admission will actually check against, and a second estimate maintained in the gateway would
    drift from it and produce a console that says `pass` where the coordinator says refuse.
    """
    for engine in engines or []:
        if engine.get("engine_id") == required_engine:
            return engine.get("est_vram_gb")
    return None


# -- jobs, runs, experiments (T726/T727) -----------------------------------------------------------

@router.get("/console/jobs")
async def console_jobs(limit: int = Query(100, ge=1, le=500)):
    """The unified active-work list (FR-372): gateway lane ⋈ agent table ⋈ tracking runs.

    One unit of work carries three identifiers on this platform. An operator investigating a stuck
    fine-tune should not have to hold all three and query three systems, which is what this join
    replaces.
    """
    projection = console.Projection()
    gateway_jobs, agent_jobs, runs = await asyncio.gather(
        projection.read("gateway", lambda: sources.broker_jobs(limit)),
        projection.read("store", lambda: sources.jobs(limit)),
        projection.read("tracking", lambda: sources.tracking_runs(limit)))

    observed = {name: (stamp, time.time()) for name, stamp in projection.observed.items()}
    rows = jobs_mod.join(gateway_jobs=gateway_jobs, agent_jobs=agent_jobs, tracking_runs=runs,
                         observed=observed)
    # A source that did not answer must not be summarized away: `null` rather than an empty list is
    # what stops the console from rendering "no work in flight" during an outage.
    return projection.envelope(rows[:limit] if projection.observed else None)


@router.get("/console/jobs/{job_id}")
async def console_job(job_id: str):
    """One `PlatformJob` with its timeline, resources, and any `StateConflict` (FR-391/393/394)."""
    projection = console.Projection()
    gateway_jobs, agent_jobs, runs = await asyncio.gather(
        projection.read("gateway", sources.broker_jobs),
        projection.read("store", sources.jobs),
        projection.read("tracking", sources.tracking_runs))

    observed = {name: (stamp, time.time()) for name, stamp in projection.observed.items()}
    match = [row for row in jobs_mod.join(gateway_jobs=gateway_jobs, agent_jobs=agent_jobs,
                                          tracking_runs=runs, observed=observed)
             if row["id"] == job_id]
    if not match:
        return projection.envelope(None)

    row = match[0]
    agent_record = next((j for j in agent_jobs or [] if j.get("job_id") == job_id), {})
    row["timeline"] = [
        {"at": agent_record.get(field), "event": event}
        for field, event in (("submitted_at", "submitted"), ("started_at", "started"),
                             ("ended_at", "ended"))
        if agent_record.get(field) is not None]
    row["resources"] = {"device_index": agent_record.get("device_index"),
                        "vram_gb": agent_record.get("vram_gb"),
                        "host": row.get("assignedHost")}
    return projection.envelope(row)


@router.get("/console/runs")
async def console_runs(limit: int = Query(100, ge=1, le=500)):
    """Run listing — net-new. Only `GET /runs/{id}` existed before 027."""
    projection = console.Projection()
    runs = await projection.read("tracking", lambda: sources.tracking_runs(limit))
    return projection.envelope(runs)


@router.get("/console/experiments")
async def console_experiments():
    projection = console.Projection()
    return projection.envelope(await projection.read("tracking", sources.experiments))


@router.get("/console/studies/{study_id}/trials")
async def console_study_trials(study_id: str):
    """The trial table, objective history, and parameter importance (FR-396).

    Reported as **recorded executions**, never as the state of a running search (FR-397). There is
    no persistent search service on this platform: a study is a sequence of trainings that already
    happened, and a view implying an optimizer is still thinking would invite an operator to wait
    for a next trial that nobody scheduled.
    """
    projection = console.Projection()
    study = await projection.read("trainer", lambda: sources.study(study_id))
    if study is None:
        return projection.envelope(None)
    return projection.envelope(studies.trial_view(study))


# -- evaluations, gates, drift (T737/T738) ----------------------------------------------------------

@router.get("/console/evaluations")
async def console_evaluations(model: str = None, limit: int = Query(100, ge=1, le=500)):
    """`EvaluationResult[]` with **modality-native** metrics (FR-398/399).

    Nothing here computes a cross-modality score. A WER, an accuracy and a perplexity are not
    comparable, and a single "score" column would produce a leaderboard ranking an ASR model against
    a classifier.
    """
    projection = console.Projection()
    versions = await projection.read("registry", sources.model_versions)
    if versions is None:
        return projection.envelope(None)

    rows = []
    for version in versions[:limit]:
        if model and version.get("name") != model:
            continue
        record = await projection.read(
            "registry",
            lambda v=version: sources.evaluation_for(v["name"], v["version"]))
        rows.append(evaluations.evaluation_result(
            name=version.get("name"), version=version.get("version"), evaluation=record,
            modality=(version.get("tags") or {}).get("task")))
    return projection.envelope(rows)


@router.get("/console/evaluations/{name}/{version}")
async def console_evaluation(name: str, version: str):
    """One evaluation with the evidence a failure needs (FR-400/401 / SC-191): the failing rule, the
    observed value, the incumbent it was compared against, and any override with its reason — all
    without leaving the view."""
    projection = console.Projection()
    record = await projection.read("registry", lambda: sources.evaluation_for(name, version))
    verdict = await projection.read("registry", lambda: sources.gate_verdict(name, version))
    return projection.envelope(evaluations.evaluation_result(
        name=name, version=version, evaluation=record, verdict=verdict))


@router.get("/console/gates")
async def console_gates():
    """The configured gate and its rules. Read from the gate's own configuration, so the console
    cannot describe a policy the platform is not enforcing."""
    from .. import evaluation as evaluation_mod

    return console.envelope({
        "mode": evaluation_mod.GATE_MODE,
        "tolerance": evaluation_mod.GATE_TOLERANCE,
        "missingMetricPolicy": evaluation_mod.GATE_MISSING_METRIC,
        "rules": [{
            "metric": "the version's own logged metric",
            "operator": "within tolerance of the incumbent, in the metric's own direction",
            "scope": "like-for-like: same modality and same metric, or the gate refuses to judge",
        }],
    })


@router.get("/console/compare")
async def console_compare(name: str, challenger: str):
    """Champion vs challenger across the six dimensions, kept **separate** (FR-403).

    A challenger that wins on quality and loses on latency is a trade-off an operator has to make;
    a combined score would make it for them silently.
    """
    from .. import registry

    projection = console.Projection()
    champion_version = await projection.read(
        "registry", lambda: registry._serving_version(registry._client(), name))
    challenger_eval = await projection.read(
        "registry", lambda: sources.evaluation_for(name, challenger))
    champion_eval = (await projection.read(
        "registry", lambda: sources.evaluation_for(name, champion_version))
        if champion_version else None)
    verdict = await projection.read("registry", lambda: sources.gate_verdict(name, challenger))

    return projection.envelope(evaluations.comparison(
        challenger={"name": name, "version": challenger},
        champion={"name": name, "version": champion_version} if champion_version else None,
        challenger_eval=challenger_eval, champion_eval=champion_eval, verdict=verdict))


@router.get("/console/drift")
async def console_drift(limit: int = Query(20, ge=1, le=100)):
    """Drift reports with their `thresholds` **inline** (FR-404/405), plus the surface's stated
    limitations (FR-406) — which are a static property of the surface, not per-report data."""
    projection = console.Projection()
    reports = await projection.read("objectstore", lambda: sources.drift_reports(limit))
    if reports is None:
        return projection.envelope(None)
    return projection.envelope({
        "reports": [evaluations.drift_report(r) for r in reports],
        "limitations": evaluations.DRIFT_LIMITATIONS,
    })


# -- inference: predictions, payload reveal, captures, traces (T742-T745) -----------------------------

@router.get("/console/predictions")
async def console_predictions(modality: str = None, model: str = None,
                              limit: int = Query(50, ge=1, le=200)):
    """Paged prediction records from the **gateway record** (FR-407), never from traces."""
    projection = console.Projection()
    rows = await projection.read(
        "store", lambda: sources.prediction_rows(limit, modality=modality, model_name=model))
    if rows is None:
        return projection.envelope(None)
    return projection.envelope([
        predictions.prediction_record(
            row, capture_state="captured" if row.get("payload_ref") else "not-captured",
            label_state="labeled" if row.get("label") else "unlabeled")
        for row in rows])


@router.get("/console/predictions/{prediction_id}")
async def console_prediction(prediction_id: str):
    """Detail with a `PayloadPreview` and **no payload content** (FR-408).

    The `preview` key is absent, not empty. A component cannot render a payload it was never sent,
    which is what makes default-hidden structural rather than a styling choice.
    """
    projection = console.Projection()
    row = await projection.read("store", lambda: sources.prediction_row(prediction_id))
    if row is None:
        return projection.envelope(None)

    record = predictions.prediction_record(
        row, capture_state="captured" if row.get("payload_ref") else "not-captured",
        label_state="labeled" if row.get("label") else "unlabeled")
    record["payload"] = predictions.payload_preview(available=bool(row.get("payload_ref")))
    return projection.envelope(record)


class RevealRequest(BaseModel):
    """The identifier travels in the **body**, never the path.

    URLs reach access logs, browser history, and `Referer` headers. A `GET /…/{id}/payload` would
    put a pointer to sensitive content in all three, permanently, for every reveal anyone performs.
    Carrying it in a body makes SC-192 structural rather than a convention someone must remember.
    """

    prediction_id: str


@router.post("/console/predictions/{prediction_id}/payload")
async def console_reveal_payload(prediction_id: str, request: RevealRequest):
    """The explicit reveal (FR-409 / SC-192). Mutates nothing.

    The path id and the body id must **match**. The path segment exists only so the route reads
    conventionally; the body is the authority, and disagreement is a client bug worth refusing
    rather than silently resolving in either direction.
    """
    if request.prediction_id != prediction_id:
        raise HTTPException(status_code=400,
                            detail="the path and body identifiers disagree")

    projection = console.Projection()
    row = await projection.read("store", lambda: sources.prediction_row(prediction_id))
    if row is None or not row.get("payload_ref"):
        return projection.envelope(predictions.payload_preview(available=False))

    content = await projection.read("objectstore",
                                    lambda: sources.payload_bytes(row["payload_ref"]))
    if content is None:
        return projection.envelope(predictions.payload_preview(available=True))
    return projection.envelope(predictions.payload_preview(
        available=True, revealed=True, content=content, total_bytes=len(content)))


@router.get("/console/captures")
async def console_captures(modality: str = None, limit: int = Query(100, ge=1, le=500)):
    projection = console.Projection()
    rows = await projection.read("store", lambda: sources.capture_rows_for(modality, limit))
    if rows is None:
        return projection.envelope(None)
    return projection.envelope([
        {"predictionId": row.get("prediction_id"), "modality": row.get("modality"),
         "modelName": row.get("model_name"), "capturedAt": row.get("captured_at"),
         "labelState": "labeled" if row.get("label") else "unlabeled",
         # The capture's object reference is NOT returned as a fetchable URL. Bytes move only
         # through the explicit reveal, which is a POST for the reason above.
         "hasPayload": bool(row.get("input_ref"))}
        for row in rows])


@router.get("/console/review-queue")
async def console_review_queue(limit: int = Query(100, ge=1, le=500)):
    """Prioritized by policy result, confidence, drift contribution, sampling, missing label,
    manual flag, and suggestion (FR-411). Every item states **which** signal put it there — a queue
    that ranks without saying why is one an operator has to take on faith."""
    projection = console.Projection()
    rows = await projection.read("store", lambda: sources.capture_rows_for(None, limit))
    if rows is None:
        return projection.envelope(None)
    return projection.envelope(predictions.review_queue(rows, limit=limit))


@router.get("/console/traces")
async def console_traces(limit: int = Query(50, ge=1, le=200)):
    projection = console.Projection()
    raw = await projection.read("tracking", lambda: sources.traces(limit))
    if raw is None:
        return projection.envelope(None)
    return projection.envelope([traces_mod.normalize(t) for t in raw])


@router.get("/console/traces/{trace_id}")
async def console_trace(trace_id: str):
    """A generic span tree (FR-412/413) — normalized server-side so the token-free presentation is
    enforced in one place and the client never learns a tracking-vendor payload shape."""
    projection = console.Projection()
    raw = await projection.read("tracking", lambda: sources.trace(trace_id))
    if raw is None:
        return projection.envelope(None)
    detail = traces_mod.normalize(raw)
    depths = traces_mod.depth_of(detail["spans"])
    for span in detail["spans"]:
        span["depth"] = depths.get(span["spanId"], 0)
    return projection.envelope(detail)


# -- deployments (T748/T749) --------------------------------------------------------------------------

@router.get("/console/endpoints")
async def console_endpoints():
    """Synthesized from registry, serving pointer, activation state, and agent engines (FR-414).

    No table and no migration (research R7): persisting this would create a fifth system of record
    whose only job is to disagree with the four it is derived from.
    """
    projection = console.Projection()

    engines_body = await runtime.engines()
    admission_body = await runtime.admission(1)
    agent_reachable = engines_body["data"] is not None
    if agent_reachable:
        projection.mark_observed(runtime.AGENT)
    else:
        projection.mark_degraded(runtime.AGENT)

    pointers = await projection.read("registry", sources.serving_pointers)
    if pointers is None and not agent_reachable:
        return projection.envelope(None)

    engines = {e.get("engine_id"): e for e in (engines_body["data"] or {}).get("engines") or []}
    admission = admission_body["data"] or {}
    job_holds_gpu = bool(admission.get("job_barrier") or admission.get("active_job"))

    rows = [
        endpoints.endpoint(
            modality=pointer.get("modality") or "unknown",
            desired=pointer,
            engine=engines.get(pointer.get("engine")),
            job_holds_gpu=job_holds_gpu,
            agent_reachable=agent_reachable)
        for pointer in pointers or []]
    return projection.envelope(rows)


@router.get("/console/endpoints/{endpoint_id}")
async def console_endpoint(endpoint_id: str):
    body = await console_endpoints()
    rows = body["data"] or []
    match = [row for row in rows if row["id"] == endpoint_id]
    return {**body, "data": match[0] if match else None}


# -- datasets and artifacts (T752/T753) ---------------------------------------------------------------

@router.get("/console/datasets")
async def console_datasets(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    """`DatasetVersion[]` (FR-419). Bytes do not flow through here — `uri` is logical, and downloads
    keep using the existing proxied route that validates permitted prefixes first."""
    projection = console.Projection()
    records = await projection.read("objectstore", sources.datasets)
    if records is None:
        return projection.envelope(None)

    rows = []
    for record in records:
        for version in record.get("versions") or []:
            rows.append(datasets_mod.dataset_version({**version, "name": record.get("name")}))
    return projection.envelope({"datasets": rows[offset:offset + limit], "total": len(rows),
                                "offset": offset, "limit": limit})


@router.get("/console/datasets/{name}/{version}")
async def console_dataset(name: str, version: str, verify: bool = False):
    """Detail plus the full validation check list.

    `verify` is opt-in (default off) because rehashing a multi-gigabyte object on every page render
    is not viable on this hardware. Unverified means `not-verified` — honest about not having
    checked rather than silently claiming it did.
    """
    projection = console.Projection()
    record = await projection.read("objectstore", lambda: sources.dataset_detail(name, version))
    if record is None:
        return projection.envelope(None)
    return projection.envelope(datasets_mod.dataset_version(
        record, validation=record.get("validation")))


@router.get("/console/artifacts")
async def console_artifacts(model: str = None, version: str = None, verify: bool = False):
    """`Artifact[]` for a model version (FR-420), with the **four** integrity states preserved.

    `not-verified` ("we did not check") never collapses into `verification-unavailable` ("no
    checksum was ever recorded") — materially different facts when deciding whether to trust an
    artifact.
    """
    projection = console.Projection()
    versions = await projection.read("registry", sources.model_versions)
    if versions is None:
        return projection.envelope(None)

    rows = []
    for entry in versions:
        if model and entry.get("name") != model:
            continue
        if version and str(entry.get("version")) != str(version):
            continue
        present = None
        if verify:
            present = await projection.read(
                "objectstore", lambda e=entry: sources.artifact_present(e.get("source")))
        rows.append(datasets_mod.artifact(
            {"uri": entry.get("source"), "kind": "model",
             "digest": (entry.get("tags") or {}).get("artifact_digest"),
             "observed_at": console.utcnow()},
            verified=False, present=present))
    return projection.envelope(rows)


# -- observability and administration (T756/T757/T758) ------------------------------------------------

@router.get("/console/metrics/summary")
async def console_metrics_summary(window_seconds: int = Query(3600, ge=60, le=86400)):
    """The curated native panels (FR-423).

    A degraded panel carries **no points** rather than zero points: a flat line at zero is a
    measurement claim, and an unreachable metrics store has not made it.
    """
    projection = console.Projection()
    window, _step = observability.bound_range(window_seconds=window_seconds, step_seconds=60)

    agent_body = await runtime.health()
    device_body = await runtime.devices()
    agent_data = agent_body["data"]
    if agent_data is None:
        projection.mark_degraded(runtime.AGENT)
    else:
        projection.mark_observed(runtime.AGENT)

    devices = (device_body["data"] or {}).get("devices") or []
    now = console.utcnow()

    def point(value):
        # A single current reading, presented as a one-point series rather than a fabricated
        # history. This deployment scrapes into Prometheus; the console does not keep its own
        # time series, and inventing one would be a chart of numbers nobody recorded.
        return [] if value is None else [{"label": "now", "points": [[0, value]]}]

    panels = [
        observability.panel("active-jobs", series=point((agent_data or {}).get("jobs_active")),
                            window_seconds=window, observed_at=now, degraded=agent_data is None),
        observability.panel(
            "gpu-utilization",
            series=point(max((d.get("utilization_pct") for d in devices
                              if d.get("utilization_pct") is not None), default=None)),
            unit="%", window_seconds=window, observed_at=now, degraded=not devices),
        observability.panel("gpu-vram", series=point((agent_data or {}).get("gpu_free_gb")),
                            unit="GB", window_seconds=window, observed_at=now,
                            degraded=agent_data is None),
        observability.panel(
            "engine-restarts",
            series=point((agent_data or {}).get("interrupted_since_start")),
            window_seconds=window, observed_at=now, degraded=agent_data is None),
    ]
    return projection.envelope({"panels": panels, "windowSeconds": window})


@router.get("/console/metrics/series")
async def console_metrics_series(key: str, window_seconds: int = Query(3600, ge=60),
                                 step_seconds: int = Query(60, ge=1)):
    """A bounded range query. The bound is **server-side**, so a hand-written URL cannot exceed it
    either — a console panel must not be able to issue an unbounded query against the metrics
    store."""
    window, step = observability.bound_range(window_seconds=window_seconds,
                                             step_seconds=step_seconds)
    if key not in observability.PANEL_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown panel key {key!r}")
    # No historical store is wired into the console (research: metrics live in Prometheus and the
    # dashboard tool reads them). `degraded` with no points, rather than an empty series that would
    # read as "measured, and it was zero".
    return console.envelope({"panel": observability.panel(
        key, window_seconds=window, observed_at=console.utcnow(), degraded=True),
        "stepSeconds": step,
        "note": "historical series are served by the dashboard tool; the console shows current state"})


@router.get("/console/alerts")
async def console_alerts():
    """Rule state only (FR-424).

    There is **no** delivery, notification, recipient, or acknowledgement field, and none may be
    added: this platform has no Alertmanager, and such a field would invite the console to imply
    someone was told. The response says so explicitly rather than leaving the reader to assume the
    usual thing.
    """
    projection = console.Projection()
    rules = await projection.read("metrics", sources.alert_rules)
    return projection.envelope({
        "rules": [observability.alert_rule(r) for r in rules or []],
        "noDeliveryNotice": observability.NO_DELIVERY_NOTICE,
    })


@router.get("/console/dashboards")
async def console_dashboards():
    """`DashboardEmbed[]` with `embeddable` resolved **server-side** from the frame policy, not
    discovered by the browser failing to render a frame."""
    return console.envelope(sources.dashboards())


@router.get("/console/admin/storage")
async def console_admin_storage():
    projection = console.Projection()
    buckets = await projection.read("objectstore", sources.bucket_summaries)
    return projection.envelope(buckets)


@router.get("/console/admin/database")
async def console_admin_database():
    """Schema version and the applied-migration ledger with its checksum state (023 US4).

    Strictly read-only. Nothing on this path triggers an apply — a console that could migrate a
    database from a page render is a console one misclick from an outage.
    """
    projection = console.Projection()
    ledger = await projection.read("database", sources.migration_ledger)
    return projection.envelope(ledger or {"schemaVersion": None, "migrations": None,
                                          "reachable": False})


@router.get("/console/admin/integrations")
async def console_admin_integrations():
    """Backing services and their reachability. `endpoint` is a **host identity**, never a
    credentialed URL — an admin page is exactly where someone would paste a screenshot."""
    projection = console.Projection()
    agent_body = await runtime.health()
    return projection.envelope(sources.integrations(agent_reachable=agent_body["data"] is not None))


@router.get("/console/admin/system")
async def console_admin_system():
    """Platform and constitution versions, host, uptime — and whether a key is configured.

    **Never** the key itself: `api_access()` reads only whether the variable is non-empty, which is
    stronger than redaction because there is no code path that touches the value.
    """
    return console.envelope({**sources.system_info(), "apiAccess": observability.api_access()})


async def _nothing():
    """An already-satisfied leg, so a skipped source still has a slot in the `gather` tuple.

    A skipped source is `[]` and **not** degraded: the search deliberately did not ask, which is
    different from asking and getting no answer.
    """
    return []


def _broker_enabled() -> bool:
    import os
    return os.getenv("BROKER_ENABLED", "1").lower() not in ("0", "false", "no")


def _co_residency_enabled() -> bool:
    import os
    return os.getenv("BROKER_COORDINATOR_ADMISSION", "0").lower() in ("1", "true", "yes", "on")


# -- runtime proxy (T714) -----------------------------------------------------------------------------

@router.get("/runtime/hosts")
async def runtime_hosts():
    """A **list** even with one host, so multi-host (FR-382) needs no later contract change."""
    return await runtime.hosts()


@router.get("/runtime/hosts/{host}/devices")
async def runtime_devices(host: str):
    return await runtime.devices(host)


@router.get("/runtime/engines")
async def runtime_engines():
    return await runtime.engines()


@router.get("/runtime/admission")
async def runtime_admission(limit: int = Query(None, ge=1, le=500)):
    return await runtime.admission(limit)


@router.get("/runtime/journal")
async def runtime_journal(cursor: str = None, limit: int = Query(100, ge=1, le=500),
                          job_id: str = None, engine_id: str = None, event_type: str = None,
                          since: float = None, until: float = None):
    """Cursor and filters pass through to the agent unchanged — the gateway does not re-page, which
    would mean two paging schemes disagreeing about what a cursor means."""
    return await runtime.journal(cursor=cursor, limit=limit, job_id=job_id, engine_id=engine_id,
                                 event_type=event_type, since=since, until=until)
