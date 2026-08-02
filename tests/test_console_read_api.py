"""027 — the gateway console read surface (T693, T698, T713, T714).

The claims here are all about **honesty under degradation**, because that is the property a
multi-backend console cannot retrofit: whether an unreachable source produces `null` or a plausible
lie, whether one dead backend fails the whole projection, and whether `mode` describes what the
deployment *is* rather than what it was configured to be.

Offline: the agent is a fake, so every degradation is producible on demand.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from gateway.app import console  # noqa: E402

OWNER_KEY = "console-owner-key"


# -- T693: the envelope ------------------------------------------------------------------------------

def test_the_envelope_carries_data_observed_degraded_and_conflict():
    body = console.envelope({"x": 1})
    assert set(body) == {"data", "observed", "degraded", "conflict"}
    assert body["data"] == {"x": 1} and body["degraded"] == [] and body["conflict"] is None
    assert body["observed"], "an observation timestamp is always present"


def test_a_projection_populates_reachable_parts_and_nulls_the_rest():
    """FR-428: a partially-degraded projection must not fail whole. A console that 503s because one
    of five sources is down tells an operator nothing about the four that are up."""
    projection = console.Projection()
    good = projection.source("registry", lambda: ["model-a"])
    bad = projection.source("agent", _boom)

    body = projection.envelope({"models": good, "devices": bad})
    assert body["data"]["models"] == ["model-a"], "the reachable part is populated"
    assert body["data"]["devices"] is None, "the unreachable part is null"
    assert body["degraded"] == ["agent"] and "registry" in body["observed"]
    assert "agent" not in body["observed"], "a source that failed has no observation time"


def test_null_is_never_serialized_as_zero_or_empty():
    """The single most consequential rule: an unreachable agent rendering as '0 devices' is a FALSE
    reading an operator would act on, not a degraded one."""
    projection = console.Projection()
    value = projection.source("agent", _boom)
    body = projection.envelope({"devices": value, "count": value})
    assert body["data"]["devices"] is None
    assert body["data"]["devices"] != [] and body["data"]["count"] != 0


def test_observed_timestamps_are_per_source():
    """A projection joining five backends has five data ages; one page-level 'as of' lies about four."""
    projection = console.Projection()
    projection.source("registry", lambda: 1)
    projection.source("store", lambda: 2)
    body = projection.envelope({})
    assert set(body["observed"]) == {"registry", "store"}


def test_a_conflict_is_reported_rather_than_resolved():
    """Research R9: picking a winner would hide that two systems of record are out of step, which is
    the thing an operator most needs to know."""
    projection = console.Projection()
    projection.conflict("model_identity",
                        {"agent": "qwen-v2", "registry": "qwen-v3"},
                        note="an activation is in flight")
    body = projection.envelope({})
    assert body["conflict"][0]["values"] == {"agent": "qwen-v2", "registry": "qwen-v3"}
    assert "in flight" in body["conflict"][0]["note"]


def test_no_conflict_serializes_as_null_not_an_empty_list():
    assert console.Projection().envelope({})["conflict"] is None


def test_a_degraded_source_is_named_only_once():
    projection = console.Projection()
    projection.source("agent", _boom)
    projection.source("agent", _boom)
    assert projection.envelope({})["degraded"] == ["agent"]


def _boom():
    raise RuntimeError("unreachable")


# -- T713: the agent proxy ----------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GATEWAY_API_KEYS", OWNER_KEY)
    # A loopback DSN so an unreachable store fails FAST. Left at the default (`postgres`) the probe
    # blocks on DNS for the resolver's whole timeout, which turns "the store is down" — the state
    # half these tests exist to exercise — into a hung suite.
    monkeypatch.setenv("GATEWAY_DB_URL",
                       "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    monkeypatch.setenv("BROKER_ENABLED", "0")
    # The gateway's startup applies migrations with five retries and an escalating sleep. Against an
    # unreachable DSN that is ~30s of pure waiting per TestClient — and this suite has no schema to
    # migrate, since every backend it touches is a fake.
    monkeypatch.setenv("GATEWAY_MIGRATIONS_ENABLED", "0")
    from gateway.app import auth
    monkeypatch.setattr(auth, "_KEY_HASHES", [auth._hash(OWNER_KEY)])
    monkeypatch.setattr(auth, "_KEYED", True)
    monkeypatch.setattr(auth, "_MODE", "keyed")
    from gateway.app.main import app
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-API-Key": OWNER_KEY}


class FakeAgent:
    """Stands in for the host agent. `up=False` makes every read fail, which is the whole point."""

    def __init__(self, up=True, payloads=None):
        self.up = up
        self.payloads = payloads or {}
        self.calls = []

    async def __call__(self, path, params=None):
        self.calls.append((path, params))
        if not self.up:
            return None, False
        return self.payloads.get(path, {}), True


@pytest.fixture()
def agent(monkeypatch):
    from gateway.app import runtime

    fake = FakeAgent(payloads={
        "/runtime/devices": {"observed_at": "2026-07-31T09:00:00Z", "source": "nvml",
                             "devices": [{"index": 0, "name": "RTX 5070 Ti",
                                          "total_vram_gb": 12.0, "free_vram_gb": 7.4}]},
        "/runtime/admission": {"observed_at": "2026-07-31T09:00:00Z", "residents": [],
                               "usable_budget_gb": 11.0, "records": []},
        "/journal": {"entries": [], "next_cursor": None, "has_more": False},
        "/engines": {"engines": [{"engine_id": "llm", "state": "ready"}]},
        "/health": {"ok": True, "engines": {"llm": "ready"}, "gpu_free_gb": 7.4, "jobs_active": 0,
                    "wedged": False, "interrupted_since_start": 0},
    })
    monkeypatch.setattr(runtime, "_get", fake)
    return fake


def test_runtime_reads_carry_the_agent_observation_when_it_is_up(client, agent):
    body = client.get("/runtime/hosts/local/devices", headers=_auth()).json()
    assert body["data"]["source"] == "nvml"
    assert body["degraded"] == [] and "agent" in body["observed"]


def test_agent_loss_returns_null_never_an_empty_list(client, agent):
    """The console would legitimately render `[]` as 'no devices', which during an outage is false
    rather than merely incomplete."""
    agent.up = False
    response = client.get("/runtime/hosts/local/devices", headers=_auth())
    assert response.status_code == 200, "a dead agent is not a request failure"
    body = response.json()
    assert body["data"] is None
    assert body["data"] != [] and body["data"] != {}
    assert body["degraded"] == ["agent"]
    assert body["observed"] == {}, "nothing was observed, so nothing carries a timestamp"


@pytest.mark.parametrize("path", ["/runtime/hosts", "/runtime/hosts/local/devices",
                                  "/runtime/engines", "/runtime/admission", "/runtime/journal"])
def test_every_runtime_route_degrades_to_null_rather_than_failing(client, agent, path):
    agent.up = False
    response = client.get(path, headers=_auth())
    assert response.status_code == 200, f"{path} failed whole"
    assert response.json()["data"] is None and response.json()["degraded"] == ["agent"]


def test_hosts_returns_a_list_even_with_one_host(client, agent):
    """FR-374/382: multi-host needs no later contract change, and it costs one row today."""
    body = client.get("/runtime/hosts", headers=_auth()).json()
    assert isinstance(body["data"], list) and len(body["data"]) == 1
    assert body["data"][0]["host"] == "local" and body["data"][0]["device_count"] == 1
    assert body["data"][0]["active_engines"] == ["llm"]


def test_journal_filters_and_cursor_pass_through_unchanged(client, agent):
    """The gateway does not re-page — two paging schemes disagreeing about a cursor is worse than
    one."""
    client.get("/runtime/journal?cursor=seq:99&limit=25&job_id=j-1", headers=_auth())
    path, params = agent.calls[-1]
    assert path == "/journal"
    assert params["cursor"] == "seq:99" and params["limit"] == 25 and params["job_id"] == "j-1"


def test_the_journal_limit_is_capped_at_the_contract_bound(client, agent):
    assert client.get("/runtime/journal?limit=10000", headers=_auth()).status_code == 422


def test_runtime_routes_require_the_operator_key(client, agent):
    """These are operator reads reached through the BFF; a tenant key must not reach them, or one
    tenant could read every other tenant's runtime state."""
    assert client.get("/runtime/hosts").status_code == 401
    assert client.get("/console/health").status_code == 401


# -- T698: health and capabilities ----------------------------------------------------------------------

def test_mode_is_resolved_from_reachability_never_from_configuration(client, agent, monkeypatch):
    """A deployment declaring itself 'full' while its agent is down describes its intention, not its
    state (research R14)."""
    from gateway.app.routers import console as console_router

    monkeypatch.setattr(console_router, "_store_reachable", lambda: True)
    monkeypatch.setattr(console_router, "_registry_reachable", lambda: True)
    assert client.get("/console/health", headers=_auth()).json()["data"]["mode"] == "full"

    agent.up = False
    body = client.get("/console/health", headers=_auth()).json()
    assert body["data"]["mode"] == "degraded"
    assert body["data"]["services"]["agent"] is False
    assert "agent" in body["degraded"]


def test_mode_is_minimal_when_nothing_but_the_gateway_answers(client, agent, monkeypatch):
    from gateway.app.routers import console as console_router

    agent.up = False
    monkeypatch.setattr(console_router, "_store_reachable", _boom)
    monkeypatch.setattr(console_router, "_registry_reachable", _boom)
    body = client.get("/console/health", headers=_auth()).json()
    assert body["data"]["mode"] == "minimal"
    assert set(body["degraded"]) == {"agent", "store", "registry"}


def test_health_nulls_agent_derived_fields_when_the_agent_is_down(client, agent, monkeypatch):
    from gateway.app.routers import console as console_router

    monkeypatch.setattr(console_router, "_store_reachable", lambda: True)
    monkeypatch.setattr(console_router, "_registry_reachable", lambda: True)
    agent.up = False
    data = client.get("/console/health", headers=_auth()).json()["data"]
    assert data["gpu_free_gb"] is None and data["jobs_active"] is None
    assert data["jobs_active"] != 0, "unknown is not zero"


def test_capabilities_reports_gated_features_as_unavailable(client, agent):
    """FR-433/418: the interface OMITS an unsupported control rather than rendering one that fails.
    Tenant jobs and sessions are gated in 026 on a native-Linux host plus a constitution
    amendment, so the console must not offer a submit button that always fails."""
    data = client.get("/console/capabilities", headers=_auth()).json()["data"]
    assert data["tenant_jobs"] is False and data["sessions"] is False
    assert data["runtime_reads"] is True


def test_capabilities_reflects_the_co_residency_flag(client, agent, monkeypatch):
    monkeypatch.setenv("BROKER_COORDINATOR_ADMISSION", "0")
    assert client.get("/console/capabilities",
                      headers=_auth()).json()["data"]["co_residency"] is False
    monkeypatch.setenv("BROKER_COORDINATOR_ADMISSION", "1")
    assert client.get("/console/capabilities",
                      headers=_auth()).json()["data"]["co_residency"] is True
