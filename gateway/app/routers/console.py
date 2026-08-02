"""Console read routes (027 T698, T714 — contracts/console-read-api.md).

Read-only projections. They add no persisted entity and require no migration (research R7).

Every route returns the envelope from `gateway/app/console/__init__.py`, so the console can render
data age per source and tell "unknown" apart from "zero". The routes are grouped here rather than
scattered across the existing routers because they are a distinct *surface* — the console's read
model — and mixing them into the lifecycle routers would blur which routes are the platform's API
and which exist to draw a screen.
"""
from fastapi import APIRouter, Query

from .. import console, runtime

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
