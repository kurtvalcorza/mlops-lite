"""026 Phase 1 — the P1 tenant inference surface and its metering (T624–T633, T678, T681, T688).

Drives the real FastAPI app against a real scratch Postgres, with only the *engine children* faked:
every claim under test is about the broker's own behaviour — who is charged, whether a racing pair of
requests can both be admitted, which status a refusal carries — and none of those depend on what a
model actually generated.

The upstream fake is deliberately small (`_FakeUpstream`), because a faithful llama.cpp stand-in
would be a second implementation to keep correct, and none of these assertions look at the text.
"""
import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from platformlib import store  # noqa: E402
from tests import _brokerdb, _gwimport  # noqa: E402

OWNER_KEY = "owner-test-key"


@pytest.fixture(scope="module", autouse=True)
def _guard():
    _brokerdb.requires_db()


@pytest.fixture(scope="module", autouse=True)
def _isolate_gateway_metrics():
    """Leave the process's Prometheus registry as this module found it.

    The repo loads `gateway/app/*.py` under two module names — the existing suites synthesize an
    `app` package, this one imports the real `gateway.app.main` — and both define the same
    module-level metrics. The global default registry refuses a second registration, so whichever
    identity comes second raises `Duplicated timeseries`, and which one that is depends on collection
    order.

    The isolation spans the whole module, not just the import, because the app registers metrics
    *after* import too: `main.py`'s startup handler lazily imports `gateway.app.scheduler`, which
    defines `gateway_policy_checks_total` the moment a `TestClient` enters its lifespan.
    """
    yield from _gwimport.isolate_module_metrics()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """A configured broker: scratch DB, owner key, plaintext allowed, a private settlement WAL."""
    with _brokerdb.ScratchDB() as db:
        monkeypatch.setenv("GATEWAY_DB_URL", db.dsn)
        monkeypatch.setenv("BROKER_ADMIN_KEYS", OWNER_KEY)
        monkeypatch.setenv("BROKER_ALLOW_PLAINTEXT", "1")
        monkeypatch.setenv("BROKER_METERING_WAL", str(tmp_path / "settlements.jsonl"))
        from gateway.app import broker as broker_mod
        broker_mod.reset_conn()
        yield db
        broker_mod.reset_conn()


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    with TestClient(_gwimport.gateway_app()) as c:
        yield c


@pytest.fixture()
def conn(env):
    with env.connect() as c:
        yield c


class _FakeUpstream:
    """Stands in for the engine children. `status` drives the refusal-mapping tests; `delay` makes a
    request's GPU-seconds measurable."""

    def __init__(self, status=200, payload=None, delay=0.0):
        self.status, self.payload, self.delay = status, payload or {}, delay
        self.calls = []

    async def __call__(self, url, payload, timeout=300.0):
        self.calls.append((url, payload))
        if self.delay:
            time.sleep(self.delay)

        class R:
            status_code = self.status
            text = json.dumps(self.payload)

            def json(_self):
                return self.payload

        return R()


@pytest.fixture()
def upstream(monkeypatch):
    from gateway.app.routers import broker_openai

    fake = _FakeUpstream(payload={"completion": "hello", "serving_model": "qwen"})
    monkeypatch.setattr(broker_openai, "_post", fake)
    return fake


def _tenant(client, name="alice", budget=None, window="daily"):
    r = client.post("/admin/tenants", json={"name": name}, headers={"X-API-Key": OWNER_KEY})
    assert r.status_code == 201, r.text
    body = r.json()
    if budget is not None:
        q = client.put(f"/admin/tenants/{body['tenant_id']}/quota",
                       json={"window": window, "budget_gpu_seconds": budget},
                       headers={"X-API-Key": OWNER_KEY})
        assert q.status_code == 200, q.text
    return body


def _auth(tenant):
    return {"Authorization": f"Bearer {tenant['api_key']}"}


# -- T624: the admin surface ---------------------------------------------------------------------------

def test_create_tenant_returns_the_raw_key_exactly_once(client):
    created = _tenant(client)
    raw = created["api_key"]
    listing = client.get("/admin/tenants", headers={"X-API-Key": OWNER_KEY}).json()
    assert raw not in json.dumps(listing), "the raw key must appear in the creation response only"


def test_rotation_invalidates_the_prior_key(client, upstream):
    created = _tenant(client)
    assert client.get("/v1/usage", headers=_auth(created)).status_code == 200
    rotated = client.post(f"/admin/tenants/{created['tenant_id']}/keys",
                          headers={"X-API-Key": OWNER_KEY}).json()
    assert client.get("/v1/usage", headers=_auth(created)).status_code == 401
    assert client.get("/v1/usage", headers=_auth(rotated)).status_code == 200


def test_admin_surface_refuses_a_tenant_key(client):
    """A tenant reaching /admin could raise its own quota — the whole reason this is a second key."""
    created = _tenant(client)
    r = client.get("/admin/usage", headers=_auth(created))
    assert r.status_code == 401


def test_admin_surface_refuses_no_credentials(client):
    assert client.get("/admin/usage").status_code == 401


# -- T625: an unmodified OpenAI client completes a request ---------------------------------------------

def test_an_unmodified_openai_client_completes_a_request(client, upstream):
    """The acceptance check as written: not a hand-rolled request that happens to match, but the
    real SDK, which validates the response against its own models."""
    openai = pytest.importorskip("openai")

    created = _tenant(client)
    sdk = openai.OpenAI(api_key=created["api_key"], base_url="http://testserver/v1",
                        http_client=client)
    completion = sdk.chat.completions.create(
        model="qwen", messages=[{"role": "user", "content": "hi"}])
    assert completion.choices[0].message.content == "hello"
    assert completion.object == "chat.completion"


def test_models_listing_is_openai_shaped(client, monkeypatch):
    from gateway.app import registry
    monkeypatch.setattr(registry, "list_models",
                        lambda: [{"name": "qwen", "serving_version": "3"},
                                 {"name": "unpromoted", "serving_version": None}])
    created = _tenant(client)
    body = client.get("/v1/models", headers=_auth(created)).json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["qwen"], \
        "a model with nothing promoted is not requestable and must not be advertised"


def test_embeddings_are_openai_shaped(client, monkeypatch):
    from gateway.app.routers import broker_openai
    monkeypatch.setattr(broker_openai, "_post", _FakeUpstream(payload=[[0.1, 0.2], [0.3, 0.4]]))
    created = _tenant(client)
    r = client.post("/v1/embeddings", json={"model": "embed", "input": ["a", "b"]},
                    headers=_auth(created))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list" and len(body["data"]) == 2
    assert body["data"][0]["embedding"] == [0.1, 0.2] and body["data"][1]["index"] == 1


def test_vision_is_task_typed(client, monkeypatch):
    from gateway.app.routers import broker_openai
    monkeypatch.setattr(broker_openai, "_post",
                        _FakeUpstream(payload={"labels": [{"label": "cat", "score": 0.9}]}))
    created = _tenant(client)
    r = client.post("/v1/vision/classify", json={"model": "vit", "image": "Zm9v"},
                    headers=_auth(created))
    assert r.status_code == 200 and r.json()["labels"][0]["label"] == "cat"
    assert client.post("/v1/vision/segment", json={"image": "Zm9v"},
                       headers=_auth(created)).status_code == 404


# -- T626: TLS is required, and refused rather than redirected -------------------------------------------

def test_plaintext_is_refused_not_redirected(client, monkeypatch, upstream):
    created = _tenant(client)  # provisioned first — the owner surface enforces TLS too
    monkeypatch.delenv("BROKER_ALLOW_PLAINTEXT", raising=False)
    r = client.get("/v1/usage", headers=_auth(created))
    assert r.status_code == 401, "plaintext must be refused"
    assert r.status_code not in (301, 302, 307, 308), \
        "a redirect would have already leaked the bearer key over http"
    assert "location" not in {k.lower() for k in r.headers}


def test_tls_is_satisfied_by_the_forwarded_proto_header(client, monkeypatch, upstream):
    """The compose deployment terminates TLS at a proxy; the app sees the hop header."""
    created = _tenant(client)
    monkeypatch.delenv("BROKER_ALLOW_PLAINTEXT", raising=False)
    r = client.get("/v1/usage", headers={**_auth(created), "X-Forwarded-Proto": "https"})
    assert r.status_code == 200


# -- T620/T627: auth and per-tenant attribution ----------------------------------------------------------

def test_missing_and_invalid_keys_are_refused_without_gpu_work(client, upstream):
    assert client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}
                       ).status_code == 401
    assert client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "x"}]},
                       headers={"Authorization": "Bearer sk-nope"}).status_code == 401
    assert upstream.calls == [], "an unauthenticated request must never reach the GPU"


def test_two_tenants_issuing_identical_requests_are_attributed_separately(client, conn, upstream):
    alice, bob = _tenant(client, "alice"), _tenant(client, "bob")
    body = {"messages": [{"role": "user", "content": "identical"}]}
    assert client.post("/v1/chat/completions", json=body, headers=_auth(alice)).status_code == 200
    assert client.post("/v1/chat/completions", json=body, headers=_auth(bob)).status_code == 200

    rows = store.list_ledger(conn)
    by_tenant = {r["tenant_id"] for r in rows}
    assert by_tenant == {alice["tenant_id"], bob["tenant_id"]}
    assert len(rows) == 2, "identical requests from two tenants are two separately attributed records"


def test_a_retried_request_id_is_charged_once(client, conn, upstream):
    """Idempotency: a client unsure whether its request landed retries with the same id."""
    alice = _tenant(client, "alice")
    body = {"messages": [{"role": "user", "content": "hi"}]}
    headers = {**_auth(alice), "X-Request-Id": "op-fixed"}
    assert client.post("/v1/chat/completions", json=body, headers=headers).status_code == 200
    assert client.post("/v1/chat/completions", json=body, headers=headers).status_code == 200
    rows = [r for r in store.list_ledger(conn) if r["ref_id"] == "op-fixed"]
    assert len(rows) == 1, "the same op id must not be billed twice"


# -- T628: the reserve step is hard and atomic --------------------------------------------------------------

def test_concurrent_reserves_against_room_for_one_grant_exactly_one(env):
    """The write-skew case: N threads, a budget with room for exactly one reservation."""
    with env.connect() as c:
        tenant = store.create_tenant(c, "racer")
        store.set_quota(c, tenant["id"], "daily", 10)

    granted, refused, errors = [], [], []
    barrier = threading.Barrier(8)

    def contend(i):
        with env.connect() as c:
            barrier.wait()
            try:
                store.reserve(c, f"op-{i}", tenant["id"], 10.0)
                granted.append(i)
            except store.QuotaExhausted:
                refused.append(i)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert len(granted) == 1, f"expected exactly one grant, got {len(granted)}"
    assert len(refused) == 7


def test_reserve_is_idempotent_for_the_same_op_id(conn):
    tenant = store.create_tenant(conn, "idem")
    store.set_quota(conn, tenant["id"], "daily", 100)
    first = store.reserve(conn, "op-1", tenant["id"], 5.0)
    second = store.reserve(conn, "op-1", tenant["id"], 5.0)
    assert first["op_id"] == second["op_id"]
    assert store.consumption(conn, tenant["id"])["outstanding_gpu_seconds"] == 5.0


def test_outstanding_reservations_count_against_the_budget(conn):
    """Counting only settled ledger rows would let a tenant with jobs in flight overshoot freely."""
    tenant = store.create_tenant(conn, "outstanding")
    store.set_quota(conn, tenant["id"], "daily", 10)
    store.reserve(conn, "op-a", tenant["id"], 8.0)
    with pytest.raises(store.QuotaExhausted):
        store.reserve(conn, "op-b", tenant["id"], 5.0)


def test_a_tenant_without_a_quota_is_recorded_but_unbounded(conn):
    tenant = store.create_tenant(conn, "unquotaed")
    store.reserve(conn, "op-x", tenant["id"], 10_000.0)
    assert store.get_reservation(conn, "op-x")["state"] == "reserved"


# -- T629: settle-to-actual, and exactly-once under a mid-settle kill ------------------------------------------

def test_settle_records_the_actual_and_releases_the_remainder(conn):
    tenant = store.create_tenant(conn, "settler")
    store.set_quota(conn, tenant["id"], "daily", 100)
    store.reserve(conn, "op-1", tenant["id"], 30.0)
    assert store.consumption(conn, tenant["id"])["consumed_gpu_seconds"] == 30.0
    store.settle(conn, "op-1", 4.5)
    state = store.consumption(conn, tenant["id"])
    assert state["settled_gpu_seconds"] == 4.5
    assert state["outstanding_gpu_seconds"] == 0.0, "the unused remainder is released"
    assert state["remaining_gpu_seconds"] == 95.5


def test_settle_is_idempotent(conn):
    tenant = store.create_tenant(conn, "twice")
    store.reserve(conn, "op-1", tenant["id"], 30.0)
    store.settle(conn, "op-1", 4.5)
    store.settle(conn, "op-1", 4.5)
    rows = [r for r in store.list_ledger(conn) if r["ref_id"] == "op-1"]
    assert len(rows) == 1 and rows[0]["gpu_seconds"] == 4.5


def test_killing_the_process_mid_settle_settles_exactly_once_on_restart(env, monkeypatch, tmp_path):
    """The WAL guarantees at-least-once; the ledger's unique ref_id collapses it to exactly-once."""
    from gateway.app import broker as broker_mod
    from gateway.app import metering

    wal = tmp_path / "wal.jsonl"
    monkeypatch.setenv("BROKER_METERING_WAL", str(wal))
    broker_mod.reset_conn()

    with env.connect() as c:
        tenant = store.create_tenant(c, "crasher")
        store.reserve(c, "op-crash", tenant["id"], 30.0)

    # The process dies after the WAL write but before the store write commits.
    def boom(*a, **k):
        raise RuntimeError("killed mid-settle")

    # Patch the store object `metering` actually holds, not the one this module imported. They are
    # usually the same, but `tests/test_store_facade.py` pops `platformlib.store` from `sys.modules`
    # and re-imports it, so a suite running after it can be holding a different module object than
    # `metering` bound at ITS import — and patching the wrong one makes this test silently assert
    # nothing. Reaching through `metering._store` is exact regardless of module-identity churn.
    monkeypatch.setattr(metering._store, "settle", boom)
    result = metering.settle("op-crash", 7.25)
    assert result["deferred"] is True, "a settle that cannot reach the store is deferred, not lost"
    assert wal.exists() and "op-crash" in wal.read_text()

    # Restart: the store is reachable again and the outbox replays.
    monkeypatch.undo()
    monkeypatch.setenv("BROKER_METERING_WAL", str(wal))
    monkeypatch.setenv("GATEWAY_DB_URL", env.dsn)
    broker_mod.reset_conn()
    replayed = metering.replay_outbox()
    assert replayed["replayed"] == 1 and replayed["failed"] == 0

    with env.connect() as c:
        rows = [r for r in store.list_ledger(c) if r["ref_id"] == "op-crash"]
        assert len(rows) == 1 and float(rows[0]["gpu_seconds"]) == 7.25

    metering.replay_outbox()  # a second replay must not double-charge
    with env.connect() as c:
        assert len([r for r in store.list_ledger(c) if r["ref_id"] == "op-crash"]) == 1


def test_a_failed_request_still_charges_what_the_gpu_spent(client, conn, monkeypatch):
    """Refunding a failed-after-load request in full would let a tenant burn the GPU for free."""
    from gateway.app.routers import broker_openai

    alice = _tenant(client, "alice")

    async def explode(url, payload, timeout=300.0):
        raise RuntimeError("child died after generating")

    monkeypatch.setattr(broker_openai, "_post", explode)
    with pytest.raises(RuntimeError):
        client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "x"}]}, headers=_auth(alice))
    reservations = store.consumption(conn, alice["tenant_id"])
    assert reservations["outstanding_gpu_seconds"] == 0.0, \
        "the reservation is resolved on the error path, never left outstanding"


# -- T630/T678: recurring windows and the window binding ---------------------------------------------------------

def test_crossing_a_window_boundary_restores_service_with_no_manual_reset(conn):
    tenant = store.create_tenant(conn, "windowed")
    store.set_quota(conn, tenant["id"], "daily", 10)
    store.reserve(conn, "op-1", tenant["id"], 10.0)
    store.settle(conn, "op-1", 10.0)
    with pytest.raises(store.QuotaExhausted):
        store.reserve(conn, "op-2", tenant["id"], 1.0)

    # Move the settled charge into yesterday's window — the same effect as the clock crossing
    # midnight, without waiting for it.
    with conn.cursor() as cur:
        cur.execute("UPDATE usage_ledger SET window_start = window_start - interval '1 day' "
                    "WHERE tenant_id = %s", (tenant["id"],))
    store.reserve(conn, "op-2", tenant["id"], 1.0)
    assert store.get_reservation(conn, "op-2")["state"] == "reserved"


def test_a_reservation_is_charged_to_the_window_that_authorized_it(conn):
    """T678: a job reserved before a boundary and settling after it charges the OLD window — the
    alternative lets the tenant spend the new window's full budget before the old job settles."""
    tenant = store.create_tenant(conn, "boundary")
    store.set_quota(conn, tenant["id"], "daily", 100)
    reservation = store.reserve(conn, "op-long", tenant["id"], 40.0)
    original_window = reservation["window_start"]

    store.settle(conn, "op-long", 40.0)
    rows = [r for r in store.list_ledger(conn) if r["ref_id"] == "op-long"]
    assert rows[0]["window_start"] == original_window, \
        "the ledger row must bear the reservation's window, not the window at completion"


def test_window_consumption_is_derived_from_window_start_not_ts(conn):
    tenant = store.create_tenant(conn, "derived")
    store.set_quota(conn, tenant["id"], "daily", 100)
    store.reserve(conn, "op-1", tenant["id"], 10.0)
    store.settle(conn, "op-1", 10.0)
    # A row whose wall-clock `ts` is today but whose window is yesterday must NOT count today.
    with conn.cursor() as cur:
        cur.execute("UPDATE usage_ledger SET window_start = window_start - interval '1 day' "
                    "WHERE ref_id = 'op-1'")
    assert store.consumption(conn, tenant["id"])["settled_gpu_seconds"] == 0.0


# -- T631: the reconciliation identity ---------------------------------------------------------------------------

def test_reconciliation_holds_over_randomized_reserve_settle_release_sequences(conn):
    """The property T631 names: ledger totals equal the sum of settled reservations, whatever
    interleaving of reserve / settle / release / abandon produced them."""
    import random

    rng = random.Random(20260802)
    tenant = store.create_tenant(conn, "property")
    for i in range(120):
        op = f"op-{i}"
        store.reserve(conn, op, tenant["id"], rng.uniform(0.0, 20.0))
        action = rng.choice(["settle", "release", "abandon", "settle", "settle"])
        if action == "settle":
            store.settle(conn, op, rng.uniform(0.0, 20.0))
        elif action == "release":
            store.release(conn, op)
        # "abandon" leaves it outstanding, which is exactly what an in-flight request looks like

    result = store.reconciliation(conn, tenant["id"])
    assert result["reconciled"], result


def test_released_reservations_never_reach_the_ledger(conn):
    tenant = store.create_tenant(conn, "released")
    store.reserve(conn, "op-1", tenant["id"], 5.0)
    store.release(conn, "op-1")
    assert [r for r in store.list_ledger(conn) if r["ref_id"] == "op-1"] == []
    assert store.consumption(conn, tenant["id"])["consumed_gpu_seconds"] == 0.0


# -- T632: the single-resident serving path is preserved ------------------------------------------------------------

def test_the_serving_path_is_a_plain_proxy_to_the_existing_child(client, upstream):
    """No new serving path: the broker adapts the surface and reuses the child's /infer verbatim."""
    alice = _tenant(client, "alice")
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "system", "content": "be terse"},
                                       {"role": "user", "content": "hi"}],
                          "max_tokens": 16, "temperature": 0.1},
                    headers=_auth(alice))
    assert r.status_code == 200
    url, payload = upstream.calls[0]
    assert url.endswith("/infer")
    assert payload["max_tokens"] == 16 and payload["temperature"] == 0.1
    assert "be terse" in payload["prompt"] and "hi" in payload["prompt"]


# -- T633: cross-tenant refusals ----------------------------------------------------------------------------------

def test_a_tenant_cannot_read_another_tenants_usage_or_keys(client, upstream):
    """The IDOR probe suite. Every admin route is owner-only, so a tenant key gets 401 on each —
    an authorization refusal, not a filtered empty result that looks like 'no data'."""
    alice, bob = _tenant(client, "alice"), _tenant(client, "bob")
    probes = [
        ("get", f"/admin/usage?tenant={bob['tenant_id']}"),
        ("get", "/admin/tenants"),
        ("get", "/admin/queue"),
        ("post", f"/admin/tenants/{bob['tenant_id']}/keys"),
        ("post", f"/admin/tenants/{bob['tenant_id']}/revoke"),
        ("put", f"/admin/tenants/{alice['tenant_id']}/quota"),
    ]
    for method, path in probes:
        r = getattr(client, method)(path, headers=_auth(alice),
                                    **({"json": {"window": "daily", "budget_gpu_seconds": 10**9}}
                                       if method == "put" else {}))
        assert r.status_code == 401, f"{method.upper()} {path} leaked to a tenant key ({r.status_code})"


def test_a_tenants_own_usage_route_is_scoped_by_construction(client, conn, upstream):
    alice, bob = _tenant(client, "alice"), _tenant(client, "bob")
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]},
                headers=_auth(bob))
    mine = client.get("/v1/usage", headers=_auth(alice)).json()
    assert mine["tenant"] == "alice"
    assert (mine["consumed_gpu_seconds"] or 0) == 0.0, "alice must not see bob's consumption"


def test_require_own_tenant_refuses_with_not_found(client):
    """404 rather than 403: 403 confirms the id exists, which is an enumeration oracle."""
    from gateway.app.tenancy import require_own_tenant

    with pytest.raises(Exception) as excinfo:
        require_own_tenant({"id": "a"}, "b")
    assert excinfo.value.status_code == 404


# -- T681: streaming usage, and no X-GPU-Seconds header --------------------------------------------------------------

def test_streamed_completion_reports_usage_as_a_terminal_event_not_a_header(client, monkeypatch):
    """Headers precede the body, so a settled X-GPU-Seconds cannot exist at flush time."""
    import httpx


    class FakeStream:
        status_code = 200

        async def aiter_lines(self):
            for token in ("Hel", "lo"):
                yield "data: " + json.dumps({"token": token})

        async def aread(self):
            return b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            class Ctx:
                async def __aenter__(_s):
                    return FakeStream()

                async def __aexit__(_s, *a):
                    return False
            return Ctx()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    alice = _tenant(client, "alice", budget=1000)

    with client.stream("POST", "/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
                       headers=_auth(alice)) as r:
        assert r.status_code == 200
        header_names = {k.lower() for k in r.headers}
        assert "x-gpu-seconds" not in header_names, \
            "a streamed response cannot carry a settled value in a header flushed before the body"
        assert "x-quota-remaining" in header_names, "the pre-flight hint is still allowed"
        body = "".join(chunk for chunk in r.iter_text())

    assert "event: usage" in body, "final usage arrives as a terminal SSE event"
    assert body.rstrip().endswith("data: [DONE]"), "the usage event precedes [DONE]"
    usage_line = [ln for ln in body.splitlines()
                  if ln.startswith("data:") and "gpu_seconds" in ln][0]
    usage = json.loads(usage_line[5:])
    assert "gpu_seconds" in usage and "quota_remaining" in usage


def test_non_streamed_completion_still_carries_the_settled_header(client, upstream):
    alice = _tenant(client, "alice", budget=1000)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]}, headers=_auth(alice))
    assert r.status_code == 200
    assert "X-GPU-Seconds" in r.headers and float(r.headers["X-GPU-Seconds"]) >= 0.0
    assert "X-Quota-Remaining" in r.headers


# -- T688: one status per refusal code -------------------------------------------------------------------------------

@pytest.mark.parametrize("upstream_status", [409, 503, 507])
def test_every_transient_cause_surfaces_as_503_gpu_busy_with_retry_after(client, monkeypatch,
                                                                        upstream_status):
    """A client retrying on 503 must eventually succeed for every transient cause — so no transient
    cause may surface as anything else."""
    from gateway.app.routers import broker_openai
    monkeypatch.setattr(broker_openai, "_post",
                        _FakeUpstream(status=upstream_status, payload={"detail": "busy"}))
    alice = _tenant(client, "alice")
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]}, headers=_auth(alice))
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "gpu_busy"
    assert "retry-after" in {k.lower() for k in r.headers}


def test_contention_never_surfaces_as_413(client, monkeypatch):
    """413 tells a client to give up. A request blocked by another tenant would have succeeded."""
    from gateway.app.routers import broker_openai
    for status in (409, 503, 507):
        monkeypatch.setattr(broker_openai, "_post",
                            _FakeUpstream(status=status, payload={"detail": "contended"}))
        alice = _tenant(client, f"t{status}")
        r = client.post("/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hi"}]},
                        headers=_auth(alice))
        assert r.status_code != 413


def test_model_too_large_is_413_and_carries_no_retry_after(client, monkeypatch):
    from gateway.app.routers import broker_openai
    monkeypatch.setattr(broker_openai, "_post",
                        _FakeUpstream(status=413, payload={"detail": "estimate exceeds capacity"}))
    alice = _tenant(client, "alice")
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]}, headers=_auth(alice))
    assert r.status_code == 413
    assert r.json()["detail"]["error"]["code"] == "model_too_large"
    assert "retry-after" not in {k.lower() for k in r.headers}, \
        "a permanent refusal must not invite a retry"


def test_quota_exhausted_is_403(client, upstream):
    alice = _tenant(client, "alice", budget=1)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]}, headers=_auth(alice))
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "quota_exhausted"


def test_a_refused_request_never_reaches_the_gpu(client, upstream):
    alice = _tenant(client, "alice", budget=1)
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]},
                headers=_auth(alice))
    assert upstream.calls == [], "quota is checked before the GPU, not after"


def test_one_tenants_exhaustion_does_not_affect_another(client, upstream):
    poor = _tenant(client, "poor", budget=1)
    rich = _tenant(client, "rich", budget=100_000)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", json=body, headers=_auth(poor)).status_code == 403
    assert client.post("/v1/chat/completions", json=body, headers=_auth(rich)).status_code == 200
