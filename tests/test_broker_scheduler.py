"""026 Phase 3 — shape lanes, the persisted jobs lane, and anti-starvation (T646–T652, T680, T685).

The lane-ordering claims are database claims (they must survive a restart), so those run against a
real scratch Postgres. The anti-starvation and drain-mode claims are pure scheduler logic and run
against a stub coordinator with an injected clock, because "a job starts within its wait bound" is a
statement about *time* and a real clock would make it either slow or flaky.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from hostagent import coordinator as co  # noqa: E402
from hostagent import scheduler as sched  # noqa: E402
from platformlib import store  # noqa: E402
from tests import _brokerdb  # noqa: E402

GIB = 1024 ** 3


# -- scheduler logic (no database) -----------------------------------------------------------------

class StubCoordinator:
    """Records what the scheduler asked of it. The coordinator's own behaviour is pinned by
    `test_agent_coordinator.py`; here only the *ordering decisions* are under test."""

    def __init__(self, serving_ok=True, job_ok=True):
        self.serving_ok = serving_ok
        self.job_ok = job_ok
        self.serving_calls = []
        self.job_calls = []
        self.ended = []

    def admit_serving(self, model_key, est_bytes, *, op_id=None, deadline=None):
        self.serving_calls.append(model_key)
        if not self.serving_ok:
            return co.Refuse(co.GPU_BUSY, "stub refuses")
        return co.Share(type("C", (), {"release": lambda s: None, "model_key": model_key})())

    def admit_job(self, job_id, deadline=None):
        self.job_calls.append(job_id)
        return self.job_ok

    def end_job(self, job_id=None):
        self.ended.append(job_id)

    def snapshot(self):
        return {"resident": [], "reservations": [], "vram": {}, "active_job": None,
                "job_barrier": False}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_inference_is_favoured_while_no_job_is_waiting():
    clock = FakeClock()
    s = sched.Scheduler(StubCoordinator(), inference_burst=3, head_job_wait_s=10, clock=clock)
    for _ in range(50):
        assert s.admit_serving("a", 1 * GIB).ok
    assert s.drain_mode() is False, "with no queued job there is nothing to starve"


def test_a_continuous_inference_load_does_not_starve_a_queued_job_burst_bound():
    """T648: the burst bound. The v1 design emitted a warning here, which observes starvation
    rather than preventing it."""
    clock = FakeClock()
    s = sched.Scheduler(StubCoordinator(), inference_burst=3, head_job_wait_s=1000, clock=clock)
    s.note_job_queued("job-1")

    for _ in range(3):
        assert s.admit_serving("a", 1 * GIB).ok
    result = s.admit_serving("a", 1 * GIB)
    assert not result.ok, "the burst bound engaged job-drain mode"
    assert result.code == co.GPU_BUSY and result.retry_after is not None


def test_a_continuous_inference_load_does_not_starve_a_queued_job_wait_bound():
    """The wait bound, which is the one that makes the guarantee a TIME rather than a probability:
    a trickle of inference below the burst count would otherwise stall a job indefinitely."""
    clock = FakeClock()
    s = sched.Scheduler(StubCoordinator(), inference_burst=10_000, head_job_wait_s=60, clock=clock)
    s.note_job_queued("job-1")

    assert s.admit_serving("a", 1 * GIB).ok
    clock.advance(59)
    assert s.admit_serving("a", 1 * GIB).ok, "still inside the wait bound"
    clock.advance(2)
    assert not s.admit_serving("a", 1 * GIB).ok, "the wait bound engaged job-drain mode"


def test_drain_mode_stops_new_inference_but_never_preempts():
    """Drain mode is about *admission*. Nothing in this design preempts an in-flight request."""
    stub = StubCoordinator()
    clock = FakeClock()
    s = sched.Scheduler(stub, inference_burst=1, head_job_wait_s=1000, clock=clock)
    s.note_job_queued("job-1")
    s.admit_serving("a", 1 * GIB)
    before = len(stub.serving_calls)
    s.admit_serving("a", 1 * GIB)
    assert len(stub.serving_calls) == before, \
        "a refused admission never reaches the coordinator, so no running request is disturbed"


def test_drain_mode_clears_once_the_head_job_starts():
    clock = FakeClock()
    s = sched.Scheduler(StubCoordinator(), inference_burst=1, head_job_wait_s=1000, clock=clock)
    s.note_job_queued("job-1")
    s.admit_serving("a", 1 * GIB)
    assert s.drain_mode() is True

    assert s.admit_head_job("job-1") is True
    assert s.drain_mode() is False
    assert s.admit_serving("a", 1 * GIB).ok, "inference resumes once the job has the GPU"


def test_the_wait_clock_restarts_for_the_next_head_job():
    clock = FakeClock()
    s = sched.Scheduler(StubCoordinator(), inference_burst=2, head_job_wait_s=1000, clock=clock)
    s.note_job_queued("job-1")
    s.note_job_queued("job-2")
    s.admit_serving("a", 1 * GIB)
    s.admit_serving("a", 1 * GIB)
    assert s.drain_mode() is True
    s.admit_head_job("job-1")
    assert s.drain_mode() is False, "job-2 is now head, with a fresh budget"
    s.admit_serving("a", 1 * GIB)
    s.admit_serving("a", 1 * GIB)
    assert s.drain_mode() is True, "and its own bound engages in turn"


def test_ending_a_job_resets_the_lane_state():
    clock = FakeClock()
    s = sched.Scheduler(StubCoordinator(), inference_burst=1, head_job_wait_s=1000, clock=clock)
    s.note_job_queued("job-1")
    s.admit_serving("a", 1 * GIB)
    s.admit_head_job("job-1")
    s.end_job("job-1")
    assert s.drain_mode() is False
    assert s.admit_serving("a", 1 * GIB).ok


# -- the persisted lane (database) ------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _guard():
    _brokerdb.requires_db()


@pytest.fixture()
def conn():
    with _brokerdb.ScratchDB() as db:
        with db.connect() as c:
            yield c


@pytest.fixture()
def tenant(conn):
    return store.create_tenant(conn, "lane-tenant")


def test_ordering_within_the_jobs_lane_is_arrival_ordered(conn, tenant):
    ids = [store.enqueue_broker_job(conn, tenant["id"], "batch", {"n": i})["id"] for i in range(5)]
    assert [j["id"] for j in store.list_queued_broker_jobs(conn)] == ids
    assert [j["queue_pos"] for j in store.list_queued_broker_jobs(conn)] == [1, 2, 3, 4, 5]


def test_a_restart_mid_queue_preserves_head_of_line_order(conn, tenant):
    """T647: FIFO that does not survive a restart is not FIFO — a tenant second in line would
    silently lose its position on every agent restart."""
    ids = [store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"] for _ in range(4)]
    store.start_broker_job(conn, ids[0])

    recovery = store.recover_broker_lane(conn)  # the "restart"

    assert [j["id"] for j in store.list_queued_broker_jobs(conn)] == ids[1:], \
        "queued jobs keep their original order — they never occupied the GPU"
    assert [i["id"] for i in recovery["interrupted"]] == [ids[0]]


def test_the_running_job_resolves_to_interrupted_not_failed(conn, tenant):
    """T685: in a metered broker the difference is the tenant's basis for disputing a charge, so it
    cannot be inferred from logs after the fact."""
    job_id = store.enqueue_broker_job(conn, tenant["id"], "finetune", {})["id"]
    store.start_broker_job(conn, job_id)
    store.recover_broker_lane(conn)
    recovered = store.get_broker_job(conn, job_id)
    assert recovered["state"] == "interrupted"
    assert recovered["state"] != "failed", \
        "a broker-caused restart must be distinguishable from a tenant-code failure"


def test_a_queued_job_is_never_swept_to_interrupted(conn, tenant):
    """The agent's own startup path rewrites every queued job to `interrupted`; broker jobs are
    recovered explicitly precisely so that sweep does not silently empty the lane on every boot."""
    job_id = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    store.recover_broker_lane(conn)
    assert store.get_broker_job(conn, job_id)["state"] == "queued"


def test_a_running_job_is_never_forced_back_to_queued(conn, tenant):
    job_id = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    store.start_broker_job(conn, job_id)
    assert store.get_broker_job(conn, job_id)["queue_pos"] is None, "a running job holds no lane position"
    with pytest.raises(store.StoreError):
        store.start_broker_job(conn, job_id)  # not queued any more


def test_at_most_one_job_runs_at_a_time(conn, tenant):
    a = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    b = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    store.start_broker_job(conn, a)
    with pytest.raises(Exception):
        store.start_broker_job(conn, b)


def test_finishing_the_head_lets_the_next_job_start(conn, tenant):
    a = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    b = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    store.start_broker_job(conn, a)
    store.finish_broker_job(conn, a, "succeeded", gpu_seconds=12.5)
    started = store.start_broker_job(conn, b)
    assert started["state"] == "running"
    assert store.get_broker_job(conn, a)["gpu_seconds"] == 12.5


# -- owner override (T649) -------------------------------------------------------------------------------

def test_reordering_a_queue_with_a_running_head_leaves_the_running_job_untouched(conn, tenant):
    running = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    store.start_broker_job(conn, running)
    queued = [store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"] for _ in range(3)]

    store.reorder_broker_job(conn, queued[2], 1)
    assert [j["id"] for j in store.list_queued_broker_jobs(conn)] == [queued[2], queued[0], queued[1]]
    assert store.get_broker_job(conn, running)["state"] == "running", "never preempted by an override"


def test_a_running_job_cannot_be_reordered(conn, tenant):
    """An operator who typed the wrong id is told, not left believing a reorder happened."""
    job_id = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    store.start_broker_job(conn, job_id)
    with pytest.raises(store.StoreError, match="never"):
        store.reorder_broker_job(conn, job_id, 1)


def test_pinning_moves_a_job_to_the_head(conn, tenant):
    ids = [store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"] for _ in range(4)]
    store.pin_broker_job(conn, ids[3])
    assert [j["id"] for j in store.list_queued_broker_jobs(conn)][0] == ids[3]
    assert [j["queue_pos"] for j in store.list_queued_broker_jobs(conn)] == [1, 2, 3, 4]


def test_cancelling_compacts_the_lane(conn, tenant):
    ids = [store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"] for _ in range(4)]
    store.cancel_broker_job(conn, ids[1])
    remaining = store.list_queued_broker_jobs(conn)
    assert [j["id"] for j in remaining] == [ids[0], ids[2], ids[3]]
    assert [j["queue_pos"] for j in remaining] == [1, 2, 3]


def test_cancelling_is_idempotent(conn, tenant):
    """T679's shape: cancelling in a loop must not progressively consume the tenant's quota, which
    starts with the state transition itself being a no-op the second time."""
    job_id = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    first = store.cancel_broker_job(conn, job_id)
    second = store.cancel_broker_job(conn, job_id)
    assert first["state"] == second["state"] == "cancelled"
    assert first["ended_at"] == second["ended_at"], "the second cancel changed nothing"


def test_an_interrupted_jobs_reservation_is_settled_to_elapsed_and_the_remainder_released(conn,
                                                                                          tenant):
    """T680: left `reserved`, an interrupted job's reservation holds quota against its tenant
    forever."""
    store.set_quota(conn, tenant["id"], "daily", 1000)
    job_id = store.enqueue_broker_job(conn, tenant["id"], "finetune", {})["id"]
    store.reserve(conn, job_id, tenant["id"], 600.0, kind="job")
    store.start_broker_job(conn, job_id)
    assert store.consumption(conn, tenant["id"])["remaining_gpu_seconds"] == 400.0

    recovery = store.recover_broker_lane(conn)
    for entry in recovery["interrupted"]:
        elapsed = 42.0  # what the caller computes from started_at
        store.settle(conn, entry["id"], elapsed)
        store.finish_broker_job(conn, entry["id"], "interrupted", gpu_seconds=elapsed)

    state = store.consumption(conn, tenant["id"])
    assert state["settled_gpu_seconds"] == 42.0
    assert state["outstanding_gpu_seconds"] == 0.0, "the remainder is released, not stranded"
    assert state["remaining_gpu_seconds"] == 958.0


# -- single authority (T650/T651/T652) ------------------------------------------------------------------------

def test_policy_scheduler_is_left_functionally_unchanged():
    """T650: `gateway/app/scheduler.py` is 018's PolicyScheduler — drift/quality monitoring that
    REACTS to contention. It contains no lane ordering, no VRAM admission, and no cross-tenant
    queue, so reducing it to a facade would delete the monitoring->retrain loop while consolidating
    no scheduling."""
    src = open(os.path.join(REPO, "gateway", "app", "scheduler.py"), encoding="utf-8").read()
    assert "class PolicyScheduler" in src
    for gpu_ordering in ("admit_serving", "admit_job", "job_barrier", "vram_accounted",
                         "usable_capacity", "jobs_lane"):
        assert gpu_ordering not in src, \
            f"PolicyScheduler must not become a second GPU-ordering authority (found {gpu_ordering})"


def test_a_policy_retrain_enters_the_jobs_lane_under_the_system_tenant(conn):
    """T651: today PolicyScheduler calls the agent's /train directly, outside any lane — the real
    single-authority gap. Under this design its retrains queue like any other job."""
    system = store.ensure_system_tenant(conn)
    tenant = store.create_tenant(conn, "ordinary")

    first = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]
    retrain = store.enqueue_broker_job(conn, system["id"], "finetune", {"policy": "drift"})["id"]
    third = store.enqueue_broker_job(conn, tenant["id"], "batch", {})["id"]

    assert [j["id"] for j in store.list_queued_broker_jobs(conn)] == [first, retrain, third], \
        "FIFO by arrival, with no privileged bypass for the system tenant"

    store.start_broker_job(conn, first)
    with pytest.raises(Exception):
        store.start_broker_job(conn, retrain), "a retrain can never co-run with a tenant job"


def test_a_policy_retrains_gpu_seconds_are_metered_to_the_system_tenant(conn):
    system = store.ensure_system_tenant(conn)
    job_id = store.enqueue_broker_job(conn, system["id"], "finetune", {})["id"]
    store.reserve(conn, job_id, system["id"], 300.0, kind="job")
    store.start_broker_job(conn, job_id)
    store.settle(conn, job_id, 118.0)
    store.finish_broker_job(conn, job_id, "succeeded", gpu_seconds=118.0)

    ledger = [r for r in store.list_ledger(conn, tenant_id=system["id"])]
    assert len(ledger) == 1 and ledger[0]["gpu_seconds"] == 118.0, \
        "retrain GPU-seconds are attributed, not invisible"


def test_the_system_tenant_holds_no_privileged_admission_path(conn):
    """The point of routing retrains through a tenant is that they get no bypass — so the system
    tenant must be an ordinary row in every respect except being undeletable."""
    system = store.ensure_system_tenant(conn)
    assert system["status"] == "active" and system["is_system"] is True
    store.set_quota(conn, system["id"], "daily", 100)
    store.reserve(conn, "op-sys", system["id"], 100.0, kind="job")
    with pytest.raises(store.QuotaExhausted):
        store.reserve(conn, "op-sys-2", system["id"], 1.0, kind="job")


# -- the scheduler's snapshot feeds GET /admin/queue (T689) ------------------------------------------------------

def test_the_snapshot_carries_both_lanes(conn, tenant):
    coord, _, _ = _coordinator()
    s = sched.Scheduler(coord, store=store, conn_factory=lambda: conn)
    store.enqueue_broker_job(conn, tenant["id"], "batch", {})
    snap = s.snapshot()
    assert "jobs_lane" in snap and len(snap["jobs_lane"]) == 1
    assert snap["jobs_lane"][0]["pos"] == 1
    assert "inference_lane" in snap and snap["inference_lane"]["drain_mode"] is False
    assert "vram" in snap and "usable_capacity" in snap["vram"]


def _coordinator():
    from tests.test_agent_coordinator import make
    return make()


# -- the lane connection is reused, not reopened per read -------------------------------------------

def test_the_lane_connection_factory_reuses_one_connection(monkeypatch):
    """`Scheduler.snapshot()` backs `GET /admin/queue`, which the console polls every few seconds,
    and neither it nor `queued()` closes what the factory hands them.

    A connect-per-call factory therefore opened a Postgres connection on every poll and dropped it
    unclosed, walking the server's connection limit until nothing could connect at all. The factory
    now caches one and reopens it only after a failure — the same self-healing shape
    `gateway/app/broker.py` and `gateway/app/policies.py` use, for the same reason.
    """
    from platformlib import store as store_mod

    opened = []

    class FakeConn:
        closed = False

        def __init__(self):
            opened.append(self)

    monkeypatch.setenv("BROKER_ENABLED", "1")
    monkeypatch.setattr(store_mod, "connect", lambda *a, **k: FakeConn())
    monkeypatch.setattr(store_mod, "recover_broker_lane",
                        lambda conn: {"interrupted": [], "requeued": []})
    monkeypatch.setattr(store_mod, "list_queued_broker_jobs", lambda conn: [])

    from hostagent import main as main_mod
    monkeypatch.setattr(main_mod, "_COORDINATOR", None)
    monkeypatch.setattr(main_mod, "_RUNTIME_LIFECYCLE", None)

    _coordinator, scheduler = main_mod.build_broker()
    before = len(opened)
    for _ in range(20):
        scheduler.snapshot()
        scheduler.queued()

    assert len(opened) == before, \
        f"{len(opened) - before} extra connections opened across 40 reads — each one leaks"


def test_the_lane_connection_is_reopened_after_a_failure(monkeypatch):
    """Self-healing: a store that comes back must not need an agent restart."""
    from platformlib import store as store_mod

    class FakeConn:
        def __init__(self):
            self.closed = False

    state = {"fail": True, "opened": 0}

    def connect(*a, **k):
        if state["fail"]:
            raise store_mod.StoreError("down")
        state["opened"] += 1
        return FakeConn()

    monkeypatch.setenv("BROKER_ENABLED", "1")
    monkeypatch.setattr(store_mod, "connect", connect)
    monkeypatch.setattr(store_mod, "recover_broker_lane",
                        lambda conn: {"interrupted": [], "requeued": []})
    monkeypatch.setattr(store_mod, "list_queued_broker_jobs", lambda conn: [])

    from hostagent import main as main_mod
    monkeypatch.setattr(main_mod, "_COORDINATOR", None)
    monkeypatch.setattr(main_mod, "_RUNTIME_LIFECYCLE", None)

    _coordinator, scheduler = main_mod.build_broker()
    assert scheduler.snapshot()["jobs_lane"] == [], "a down store degrades the view, not the read"

    state["fail"] = False
    scheduler.snapshot()
    assert state["opened"] == 1, "the factory reconnects once the store is back"
