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

from fastapi import APIRouter, Query

from .. import console, runtime
from ..console import jobs as jobs_mod
from ..console import overview, sources

router = APIRouter()


# -- platform health and capabilities (T698) ---------------------------------------------------------

@router.get("/console/health")
async def console_health():
    """`PlatformHealth`, including the resolved **mode**.

    `mode` is resolved from **reachability**, never from a configured string (research R14). A
    deployment that declares itself "full" while its agent is down would be describing its
    intention rather than its state, and the console would then confidently show a degraded platform
    as healthy.
    """
    projection = console.Projection()

    agent = await runtime.health()
    agent_reachable = agent["data"] is not None
    if agent_reachable:
        projection.mark_observed("agent")
    else:
        projection.mark_degraded("agent")

    store_ok = projection.source("store", _store_reachable, default=False)
    registry_ok = projection.source("registry", _registry_reachable, default=False)

    if agent_reachable and store_ok and registry_ok:
        mode = "full"
    elif store_ok or registry_ok:
        mode = "degraded"
    else:
        mode = "minimal"

    return projection.envelope({
        "mode": mode,
        "services": {
            "agent": agent_reachable,
            "store": store_ok,
            "registry": registry_ok,
        },
        "gpu_free_gb": (agent["data"] or {}).get("gpu_free_gb") if agent_reachable else None,
        "jobs_active": (agent["data"] or {}).get("jobs_active") if agent_reachable else None,
        "wedged": (agent["data"] or {}).get("wedged") if agent_reachable else None,
    })


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
