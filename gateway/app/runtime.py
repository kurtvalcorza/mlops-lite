"""The agent runtime proxy — the gateway's only path to :8100 (027 T713, research R5).

The console never reaches the agent directly. One trust boundary, preserved: the gateway is the only
holder of `X-Agent-Key` (023 US2), and the BFF allowlist carries **no agent path**, so a browser
session cannot reach the agent even with the operator credential.

## Agent loss returns `null`, never an empty list

This is the module's load-bearing rule. When the agent is unreachable these reads return `200` with
`data: null` and `degraded: ["agent"]` — never `[]`. An empty list is a *legitimate* answer the
console would render as "no devices", "no engines", "nothing running", and an operator seeing that
during an agent outage would conclude their GPU is idle. `null` plus a named degraded source is the
only honest shape, and it is the reason the envelope exists at all.
"""
import httpx

from . import console
from .settings import AGENT_URL, agent_headers

#: Read timeouts. Short by design: these are console polls, and a console that hangs waiting on a
#: sick agent is worse than one that says "agent unreachable" and keeps rendering everything else.
TIMEOUT_S = 5.0

#: The source name used in `observed` / `degraded`. One constant so the console's degradation matrix
#: matches what these routes actually emit.
AGENT = "agent"


async def _get(path: str, params: dict = None):
    """One agent read. Returns `(payload, reachable)` — never raises to the caller.

    Raising would make a single unreachable source fail the whole projection, which is exactly the
    fail-whole behaviour FR-428 forbids.
    """
    async with httpx.AsyncClient(headers=agent_headers(), timeout=TIMEOUT_S) as client:
        try:
            response = await client.get(f"{AGENT_URL}{path}", params=params or {})
        except httpx.HTTPError:
            return None, False
    if response.status_code != 200:
        return None, False
    try:
        return response.json(), True
    except ValueError:
        return None, False


async def devices(host: str = None) -> dict:
    payload, reachable = await _get("/runtime/devices")
    return _envelope(payload, reachable)


async def admission(limit: int = None) -> dict:
    payload, reachable = await _get("/runtime/admission",
                                    {"limit": limit} if limit else None)
    return _envelope(payload, reachable)


async def journal(**filters) -> dict:
    params = {k: v for k, v in filters.items() if v is not None}
    payload, reachable = await _get("/journal", params)
    return _envelope(payload, reachable)


async def engines() -> dict:
    payload, reachable = await _get("/engines")
    return _envelope(payload, reachable)


async def health() -> dict:
    payload, reachable = await _get("/health")
    return _envelope(payload, reachable)


async def hosts() -> dict:
    """The host list — a **list even with one host** (FR-374/382).

    Returning a list for the single-host reference deployment means multi-host needs no later
    contract change, and it costs the console nothing today: it renders one row.
    """
    agent_health, reachable = await _get("/health")
    device_payload, devices_reachable = await _get("/runtime/devices")

    if not reachable:
        # `null`, not `[]` — see the module docstring. An empty host list reads as "no hosts", which
        # during an agent outage is false rather than merely incomplete.
        return console.envelope(None, observed={}, degraded=[AGENT])

    device_list = (device_payload or {}).get("devices") or []
    engines_map = (agent_health or {}).get("engines") or {}
    host = {
        "host": "local",
        "reachable": True,
        "device_count": len(device_list) if devices_reachable else None,
        "active_engines": [eid for eid, state in engines_map.items()
                           if state not in ("cold", "unavailable")],
        "jobs_active": (agent_health or {}).get("jobs_active"),
        "interrupted_since_start": (agent_health or {}).get("interrupted_since_start"),
        "wedged": (agent_health or {}).get("wedged"),
        "gpu_free_gb": (agent_health or {}).get("gpu_free_gb"),
        "last_heartbeat": console.utcnow(),
    }
    degraded = [] if devices_reachable else [AGENT]
    return console.envelope([host], observed={AGENT: console.utcnow()}, degraded=degraded)


def _envelope(payload, reachable: bool) -> dict:
    if not reachable:
        return console.envelope(None, observed={}, degraded=[AGENT])
    return console.envelope(payload, observed={AGENT: console.utcnow()})
