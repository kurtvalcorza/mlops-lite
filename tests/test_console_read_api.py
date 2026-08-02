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

from tests import _gwimport  # noqa: E402

with _gwimport.isolated_metrics():
    from gateway.app import console  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    """See `tests/test_broker_inference.py` — the isolation spans the module because the app also
    registers metrics from lazy imports during `TestClient` startup, not only at import."""
    yield from _gwimport.isolate_module_metrics()

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
    # The registry and object-store readers talk to services that are absent here and, unlike the
    # store, absent in the *hangs* sense rather than the *refuses* sense — an unresolvable host with
    # a retrying client. The projection bounds every source read; this shortens the bound so the
    # suite spends one second learning "unreachable" instead of the production five.
    monkeypatch.setattr(console, "SOURCE_TIMEOUT_S", 1.0)
    from gateway.app import auth
    monkeypatch.setattr(auth, "_KEY_HASHES", [auth._hash(OWNER_KEY)])
    monkeypatch.setattr(auth, "_KEYED", True)
    monkeypatch.setattr(auth, "_MODE", "keyed")
    with TestClient(_gwimport.gateway_app()) as c:
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


# -- T701/T702/T703: attention, activity, search --------------------------------------------------
#
# The composition rules are tested directly against `console.overview`, which takes its sources as
# plain values. That is deliberate: these rules are about what the console is allowed to CLAIM, and
# pinning them through a TestClient would mean every claim needs a live backend to state it.

NOW = "2026-07-31T09:00:00Z"


def _kinds(items):
    return {i["kind"] for i in items}


def test_a_source_that_did_not_answer_contributes_nothing_rather_than_an_all_clear():
    """The distinction the whole envelope exists for. `jobs=None` means the job table did not
    answer; it must not be read as 'no failed runs'."""
    from gateway.app.console import overview

    silent = overview.attention_items(now=NOW, jobs=None)
    empty = overview.attention_items(now=NOW, jobs=[])
    assert silent == [] and empty == []
    # Same output — which is exactly why `degraded` has to carry the difference, and why the route
    # below is tested for naming the source.


def test_every_declared_attention_kind_is_reachable():
    """A kind declared in the data model but never emitted is a promise the panel silently breaks."""
    from gateway.app.console import overview

    items = overview.attention_items(
        now=NOW,
        agent={"engines": {"llm": "wedged"}, "wedged": False, "interrupted_since_start": 2},
        admission={"records": [{"decision": "refused", "model_key": "qwen",
                                "explanation": "live-vram: needs 8.0 GB, 3.1 GB free"}]},
        jobs=[{"job_id": "j-1", "state": "failed", "kind": "finetune"}],
        drift=[{"model_name": "churn", "max_psi": 0.41, "created_at": NOW}],
        versions=[{"name": "qwen", "version": "3", "tags": {}, "artifactPresent": False,
                   "gate": {"verdict": "fail", "reason": "accuracy below threshold"}}],
        unlabeled=500,
        heartbeat_age_s=999.0)
    assert _kinds(items) == set(overview.ATTENTION_KINDS), (
        "some kind is declared but unreachable: " + str(set(overview.ATTENTION_KINDS) - _kinds(items)))


def test_items_are_ranked_by_severity_not_by_which_source_answered_first():
    from gateway.app.console import overview

    items = overview.attention_items(
        now=NOW,
        jobs=[{"job_id": "j-1", "state": "failed"}],
        agent={"engines": {"llm": "crashed"}},
        unlabeled=999)
    assert [i["severity"] for i in items] == ["critical", "warning", "info"]


def test_a_stale_heartbeat_is_a_warning_not_a_critical():
    """data-model §2: agent loss degrades. CPU modalities keep serving, and calling it critical
    overstates an outage the operator can still work through."""
    from gateway.app.console import overview

    items = overview.attention_items(now=NOW, heartbeat_age_s=overview.STALE_HEARTBEAT_S + 1)
    assert [i["kind"] for i in items] == ["stale-agent-heartbeat"]
    assert items[0]["severity"] == "warning"


def test_an_unchecked_artifact_is_not_reported_as_a_missing_one():
    """`artifactPresent: None` means the object store was not reachable to check. Reporting that as
    a missing artifact would send an operator hunting for a file that is probably there."""
    from gateway.app.console import overview

    items = overview.attention_items(now=NOW,
                                     versions=[{"name": "m", "version": "1",
                                                "tags": {"signature": "sig"},
                                                "artifactPresent": None}])
    assert "missing-artifact" not in _kinds(items)


def test_a_refusal_reuses_admissions_own_explanation_verbatim():
    """FR-378: re-wording it here would produce a second account of admission's reasoning that
    drifts from the real one."""
    from gateway.app.console import overview

    explanation = "budget: accounted 11.2 GB would exceed the 11.0 GB usable budget"
    items = overview.attention_items(
        now=NOW, admission={"records": [{"decision": "refused", "model_key": "qwen",
                                         "explanation": explanation}]})
    assert items[0]["detail"] == explanation


def test_moderate_drift_is_info_and_significant_drift_is_a_warning():
    from gateway.app.console import overview

    items = overview.attention_items(
        now=NOW, drift=[{"model_name": "a", "max_psi": 0.15, "created_at": NOW},
                        {"model_name": "b", "max_psi": 0.40, "created_at": NOW},
                        {"model_name": "c", "max_psi": 0.02, "created_at": NOW}])
    by_subject = {i["subject"]: i["severity"] for i in items}
    assert by_subject == {"a": "info", "b": "warning"}, "sub-threshold drift is not an item"


def test_activity_drops_undated_events_rather_than_placing_them_arbitrarily():
    """An event rendered at the wrong time is worse than one not rendered: the reader cannot tell."""
    from gateway.app.console import overview

    events = overview.activity_events(
        jobs=[{"job_id": "j-1", "kind": "finetune", "submitted_at": NOW, "ended_at": None},
              {"job_id": "j-2", "kind": "finetune", "submitted_at": None, "ended_at": None}])
    assert [e["subject"] for e in events] == ["j-1"]


def test_activity_is_newest_first_and_bounded():
    from gateway.app.console import overview

    jobs = [{"job_id": f"j-{i}", "kind": "finetune", "submitted_at": f"2026-07-31T09:00:{i:02d}Z"}
            for i in range(10)]
    events = overview.activity_events(jobs=jobs, limit=3)
    assert [e["subject"] for e in events] == ["j-9", "j-8", "j-7"]


def test_activity_normalizes_every_source_to_one_shape():
    """A timeline where a training job and a promotion carry different shapes forces the interface
    to special-case each source, and every new source then means a new special case."""
    from gateway.app.console import overview

    events = overview.activity_events(
        jobs=[{"job_id": "j-1", "kind": "finetune", "submitted_at": NOW}],
        versions=[{"name": "qwen", "version": "3", "created_at": NOW, "serving": True}],
        drift=[{"model_name": "churn", "max_psi": 0.3, "created_at": NOW}])
    assert len(events) >= 4
    for event in events:
        assert set(event) == {"at", "stage", "kind", "subject", "detail", "href"}
        assert event["stage"] in overview.ACTIVITY_STAGES


def test_search_puts_an_exact_id_match_first():
    """Someone pasting an id from a log wants that thing, not three things that resemble it."""
    from gateway.app.console import overview

    results = overview.search_results(
        "j-12", jobs=[{"job_id": "j-123", "kind": "finetune"}, {"job_id": "j-12", "kind": "hpo"}])
    assert [r["id"] for r in results] == ["j-12", "j-123"]


def test_search_spans_every_declared_kind():
    from gateway.app.console import overview

    results = overview.search_results(
        "alpha",
        models=[{"name": "alpha-llm"}],
        runs=[{"run_id": "r1", "name": "alpha-run"}],
        datasets=[{"name": "alpha-data"}],
        jobs=[{"job_id": "alpha-job", "kind": "finetune"}],
        endpoints=[{"id": "alpha-ep", "name": "alpha"}],
        predictions=[{"prediction_id": "alpha-pred"}])
    assert {r["kind"] for r in results} == set(overview.SEARCH_KINDS)


def test_search_ignores_an_empty_query_rather_than_returning_everything():
    from gateway.app.console import overview

    assert overview.search_results("  ", models=[{"name": "a"}]) == []


def test_only_an_identifier_shaped_query_reaches_the_prediction_table():
    """The prediction table is the largest on the platform; a two-character fragment scanning it
    would make the search box the most expensive control in the console."""
    from gateway.app.console import overview

    assert overview.looks_like_id("7f3a19c4-2b8e-4d51-9a77-0e2c1f4b8d90")
    assert overview.looks_like_id("pred-000000123456")
    assert not overview.looks_like_id("qwen")
    assert not overview.looks_like_id("churn model")


def test_attention_names_its_unreachable_sources(client, agent, monkeypatch):
    """The route half of the first test in this section: identical items, but the envelope says
    which sources produced them and which never answered."""
    agent.up = False
    body = client.get("/console/attention", headers=_auth()).json()
    assert body["data"] == [] or isinstance(body["data"], list)
    assert "agent" in body["degraded"], "an unreachable agent must be named, not silently omitted"


@pytest.mark.parametrize("path", ["/console/attention", "/console/activity", "/console/search?q=x"])
def test_the_overview_projections_survive_every_source_being_down(client, agent, path):
    """FR-428 again, on the surface an operator reaches for first during an outage."""
    agent.up = False
    response = client.get(path, headers=_auth())
    assert response.status_code == 200, f"{path} failed whole"
    assert response.json()["degraded"], "a fully-degraded read must say so"


def test_search_requires_a_query(client, agent):
    assert client.get("/console/search", headers=_auth()).status_code == 422


# -- bounded source reads -------------------------------------------------------------------------
#
# Every source behind a console projection is a network call to something that can be *sick* rather
# than *down*, and a sick backend hangs rather than refusing. These pin the two properties that makes
# survivable: the request gives up, and the process can still exit.

@pytest.mark.anyio
async def test_a_source_that_never_answers_is_reported_degraded_rather_than_hanging():
    import threading

    release = threading.Event()
    projection = console.Projection()
    try:
        value = await projection.read("registry", release.wait, default=None, timeout_s=0.05)
    finally:
        release.set()
    assert value is None and projection.degraded == ["registry"]
    assert "registry" not in projection.observed, "nothing was observed, so nothing is stamped"


@pytest.mark.anyio
async def test_an_abandoned_source_read_runs_on_a_daemon_thread():
    """A `ThreadPoolExecutor` joins its workers at exit, so one read stuck against a sick backend
    would hold the gateway open at shutdown — a worse failure than the degradation the timeout
    exists to report. The abandoned thread must not be able to do that."""
    import threading

    release = threading.Event()
    seen = {}

    def blocking():
        seen["daemon"] = threading.current_thread().daemon
        release.wait()

    projection = console.Projection()
    try:
        await projection.read("store", blocking, timeout_s=0.05)
        for _ in range(100):
            if "daemon" in seen:
                break
            await _sleep()
        assert seen.get("daemon") is True
    finally:
        release.set()


@pytest.mark.anyio
async def test_reads_past_the_inflight_cap_are_refused_rather_than_queued():
    """The console polls. Queueing behind a permanently hung backend converts one slow source into
    a slow console, and grows one abandoned thread per poll."""
    import threading

    release = threading.Event()
    projection = console.Projection()
    try:
        for _ in range(console.MAX_INFLIGHT):
            await projection.read("store", release.wait, timeout_s=0.02)
        # The cap is now full of abandoned reads; the next one must fail fast, not wait.
        import time
        started = time.monotonic()
        assert await projection.read("registry", lambda: "fresh", timeout_s=5) is None
        assert time.monotonic() - started < 1.0, "a capped read waited instead of refusing"
        assert "registry" in projection.degraded
    finally:
        release.set()


async def _sleep():
    import asyncio
    await asyncio.sleep(0.01)


@pytest.fixture
def anyio_backend():
    return "asyncio"
