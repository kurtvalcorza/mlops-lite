"""027 T734 — the seven-service degradation matrix (data-model §11, FR-428 / SC-193).

The matrix is the increment's safety property written as a table: for each service, what the console
must still show and what it must stop claiming. This suite is the executable form of that table.

**Offline on purpose.** `tests/test_ui_resilience.py` exercises the same matrix against a live
console and skips when one is not up — which is every CI run. A degradation matrix that only runs
on a developer's box is a matrix nothing enforces, so the machine-checkable half lives here, over a
fake agent and monkeypatched reachability, where every row is producible on demand.

The load-bearing row is the agent one. With the agent down, runtime reads must be `unknown` and jobs
must **not** be reported stopped. An empty `devices: []` is a **failing** assertion here, not a
pass: an empty list is a legitimate answer the console renders as "no devices", and an operator
seeing that during an agent outage would conclude their GPU is idle.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from tests import _gwimport  # noqa: E402

with _gwimport.isolated_metrics():
    from gateway.app import console  # noqa: E402

OWNER_KEY = "degradation-owner-key"


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    yield from _gwimport.isolate_module_metrics()


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GATEWAY_API_KEYS", OWNER_KEY)
    monkeypatch.setenv("GATEWAY_DB_URL", "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    monkeypatch.setenv("BROKER_ENABLED", "0")
    monkeypatch.setenv("GATEWAY_MIGRATIONS_ENABLED", "0")
    monkeypatch.setattr(console, "SOURCE_TIMEOUT_S", 1.0)
    from gateway.app import auth
    monkeypatch.setattr(auth, "_KEY_HASHES", [auth._hash(OWNER_KEY)])
    monkeypatch.setattr(auth, "_KEYED", True)
    monkeypatch.setattr(auth, "_MODE", "keyed")
    with TestClient(_gwimport.gateway_app()) as c:
        yield c


class FakeAgent:
    def __init__(self, up=True):
        self.up = up

    async def __call__(self, path, params=None):
        if not self.up:
            return None, False
        return {
            "/runtime/devices": {"source": "nvml", "devices": [{"index": 0, "name": "RTX 5070 Ti"}]},
            "/runtime/admission": {"residents": [], "records": [], "usable_budget_gb": 11.0},
            "/journal": {"entries": [], "next_cursor": None, "has_more": False},
            "/engines": {"engines": [{"engine_id": "llm", "state": "ready"}]},
            "/health": {"ok": True, "engines": {"llm": "ready"}, "gpu_free_gb": 7.4,
                        "jobs_active": 2, "wedged": False, "interrupted_since_start": 0},
        }.get(path, {}), True


@pytest.fixture()
def agent(monkeypatch):
    from gateway.app import runtime

    fake = FakeAgent()
    monkeypatch.setattr(runtime, "_get", fake)
    return fake


def _auth():
    return {"X-API-Key": OWNER_KEY}


def _reachable(monkeypatch, *, store=True, registry=True, objectstore=True):
    from gateway.app.routers import console as console_router

    def boom():
        raise RuntimeError("unreachable")

    monkeypatch.setattr(console_router, "_store_reachable", (lambda: True) if store else boom)
    monkeypatch.setattr(console_router, "_registry_reachable", (lambda: True) if registry else boom)
    monkeypatch.setattr(console_router, "_objectstore_reachable",
                        (lambda: True) if objectstore else boom)


def _state(body, service):
    return next(s["state"] for s in body["data"]["services"] if s["service"] == service)


# -- the matrix, row by row (data-model §11) -------------------------------------------------------

def test_tracking_loss_degrades_and_preserves_the_rest(client, agent, monkeypatch):
    _reachable(monkeypatch, registry=False)
    body = client.get("/console/health", headers=_auth()).json()
    assert body["data"]["overall"] == "degraded"
    assert _state(body, "tracking") == "degraded"
    # Preserved: runtime still answers, because it does not come from tracking.
    assert client.get("/runtime/hosts", headers=_auth()).json()["data"] is not None


def test_database_loss_is_critical_and_names_the_shell_as_all_that_remains(client, agent,
                                                                          monkeypatch):
    _reachable(monkeypatch, store=False)
    body = client.get("/console/health", headers=_auth()).json()
    assert body["data"]["overall"] == "critical"
    assert _state(body, "database") == "critical"


def test_objectstore_loss_degrades_and_preserves_everything_else(client, agent, monkeypatch):
    _reachable(monkeypatch, objectstore=False)
    body = client.get("/console/health", headers=_auth()).json()
    assert body["data"]["overall"] == "degraded"
    assert _state(body, "objectstore") == "degraded"
    assert _state(body, "database") == "healthy" and _state(body, "agent") == "healthy"


def test_metrics_and_dashboard_loss_never_criticals(client, agent, monkeypatch):
    """The matrix's last two rows: a metrics outage degrades and a dashboard embed failure is not a
    platform health event at all — it falls back to an external link."""
    _reachable(monkeypatch)
    body = client.get("/console/health", headers=_auth()).json()
    metrics = next(s for s in body["data"]["services"] if s["service"] == "metrics")
    assert metrics["required"] is False


# -- the load-bearing row --------------------------------------------------------------------------

def test_agent_loss_degrades_and_never_criticals(client, agent, monkeypatch):
    """CPU modalities (embeddings, tabular) still serve. Calling this `critical` would overstate an
    outage the operator can still work through."""
    _reachable(monkeypatch)
    agent.up = False
    body = client.get("/console/health", headers=_auth()).json()
    assert body["data"]["overall"] == "degraded"
    assert _state(body, "agent") == "degraded"


@pytest.mark.parametrize("path", ["/runtime/hosts", "/runtime/hosts/local/devices",
                                  "/runtime/engines", "/runtime/admission", "/runtime/journal"])
def test_with_the_agent_down_runtime_reads_null_and_never_an_empty_list(client, agent, monkeypatch,
                                                                       path):
    """SC-193's sharpest edge. `devices: []` is a FAILING assertion here, not a pass: an empty list
    is a legitimate answer the console renders as 'no devices', and an operator seeing that during
    an outage would conclude their GPU is idle."""
    _reachable(monkeypatch)
    agent.up = False
    body = client.get(path, headers=_auth()).json()
    assert body["data"] is None
    assert body["data"] != [] and body["data"] != {}
    assert "agent" in body["degraded"]


def test_with_the_agent_down_jobs_are_not_reported_stopped(client, agent, monkeypatch):
    """FR-428, and the single most damaging inference this layer could make. 'We cannot see the
    work' rendered as 'the work is done' is a false all-clear during exactly the outage an operator
    is chasing."""
    from gateway.app.console import jobs as jobs_mod

    _reachable(monkeypatch)
    agent.up = False

    state = jobs_mod.normalize(gateway="running", agent="unreachable", tracking="RUNNING")
    assert state == "Unknown"
    assert state not in ("Succeeded", "Failed", "Cancelled")

    # And the card that would carry the falsehood: unknown, not zero.
    data = client.get("/console/summary", headers=_auth()).json()["data"]
    assert data["runningJobs"] is None
    assert data["runningJobs"] != 0, "unknown is not zero"


def test_with_the_agent_down_the_gpu_card_is_unknown_rather_than_zero(client, agent, monkeypatch):
    _reachable(monkeypatch)
    agent.up = False
    data = client.get("/console/summary", headers=_auth()).json()["data"]
    assert data["gpuUtilization"] is None and data["gpuUtilization"] != 0


def test_no_projection_fails_whole_with_every_backend_down(client, agent, monkeypatch):
    """FR-428: a console that 503s because its backends are down tells an operator nothing at
    exactly the moment they need it."""
    _reachable(monkeypatch, store=False, registry=False, objectstore=False)
    agent.up = False
    for path in ("/console/health", "/console/summary", "/console/attention", "/console/activity",
                 "/console/jobs", "/console/catalog", "/runtime/hosts"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200, f"{path} failed whole"
        assert response.json()["degraded"], f"{path} did not name its unreachable sources"
