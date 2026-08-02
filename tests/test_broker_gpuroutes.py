"""026 — the agent's broker GPU routes (T689, T649).

`GET /gpu/queue` and `POST /gpu/jobs/{id}/override` are on the AGENT rather than the gateway because
the agent is the sole GPU-ordering authority: the gateway holds no admission state, and synthesizing
any there would create a second, lagging answer to a question with exactly one authority.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from hostagent import gpuroutes  # noqa: E402
from hostagent import main as agent_main  # noqa: E402
from hostagent import scheduler as sched  # noqa: E402
from platformlib import store  # noqa: E402
from tests import _brokerdb  # noqa: E402
from tests.test_agent_coordinator import make  # noqa: E402

GIB = 1024 ** 3
MB = 1024 ** 2


def _scheduler(**kw):
    coord, gpu, life = make(**kw)
    return sched.Scheduler(coord), coord, gpu, life


# -- GET /gpu/queue (T689) --------------------------------------------------------------------------

def test_the_queue_payload_lets_an_operator_assert_invariant_1_by_reading_it():
    """Drill 3 step 3 reads values rather than inferring them — an earlier revision exposed only
    `vram_free_mb`, so neither bound was checkable from the documented surface."""
    s, coord, gpu, life = _scheduler(total=12 * GIB, sizes={"a": 4 * GIB})
    claim = coord.admit_serving("a", 4 * GIB).claim

    payload = gpuroutes.queue_payload(s)
    vram = payload["vram"]
    assert vram["accounted_mb"] + vram["reserved_mb"] <= vram["usable_capacity_mb"], "invariant 1"
    assert vram["usable_capacity_mb"] == pytest.approx(11 * 1024, abs=1)
    assert vram["accounted_mb"] == pytest.approx(4 * 1024, abs=1)
    assert vram["safety_headroom_mb"] == pytest.approx(512, abs=1)
    assert vram["live_free_mb"] == pytest.approx(8 * 1024, abs=1)
    claim.release()


def test_the_payload_names_every_field_the_contract_names():
    s, coord, gpu, life = _scheduler()
    payload = gpuroutes.queue_payload(s)
    assert set(payload) >= {"resident", "reservations", "vram", "inference_lane", "jobs_lane",
                            "active_job", "job_barrier"}
    assert set(payload["vram"]) == {"usable_capacity_mb", "accounted_mb", "reserved_mb",
                                    "unmaterialized_mb", "live_free_mb", "safety_headroom_mb"}


def test_reserved_and_unmaterialized_are_different_sums_and_both_present():
    """They do not take the same reservation term: invariant 1 counts EVERY outstanding reservation
    against the budget, while invariant 2 deducts only the not-yet-reconciled ones from live-free."""
    s, coord, gpu, life = _scheduler(total=12 * GIB, sizes={"a": 2 * GIB})
    with coord._locked():
        from hostagent import coordinator as co
        settled = co.Reservation("op-settled", "x", 1 * GIB, 0)
        settled.materialized = True
        coord.reservations["op-settled"] = settled
        coord.reservations["op-open"] = co.Reservation("op-open", "y", 2 * GIB, 0)

    vram = gpuroutes.queue_payload(s)["vram"]
    assert vram["reserved_mb"] == pytest.approx(3 * 1024, abs=1), "every outstanding reservation"
    assert vram["unmaterialized_mb"] == pytest.approx(2 * 1024, abs=1), "only the unreconciled one"


def test_a_loading_resident_is_visible_as_loading_not_as_a_broken_invariant():
    s, coord, gpu, life = _scheduler(sizes={"a": 2 * GIB})
    import threading
    life.release_load = threading.Event()
    threading.Thread(target=lambda: coord.admit_serving("a", 2 * GIB), daemon=True).start()
    life.load_started.wait(3)

    payload = gpuroutes.queue_payload(s)
    assert payload["resident"][0]["state"] == "loading"
    assert payload["reservations"][0]["materialized"] is False
    life.release_load.set()


def test_the_route_answers_503_when_the_broker_is_disabled():
    """`BROKER_ENABLED=0` gets an agent that behaves exactly as before — and a route that says so
    rather than inventing state."""
    from types import SimpleNamespace
    status, payload, _ = agent_main._get_gpu_queue("/gpu/queue", SimpleNamespace(scheduler=None))
    assert status == 503 and "not enabled" in payload["error"]


def test_the_route_is_registered_on_the_agent_get_table():
    matched = [h for m, h in agent_main._GET_ROUTES if m("/gpu/queue")]
    assert matched and matched[0] is agent_main._get_gpu_queue


def test_the_override_route_is_registered_on_the_agent_post_table():
    matched = [h for m, h in agent_main._POST_ROUTES if m("/gpu/jobs/j-1/override")]
    assert matched and matched[0] is agent_main._post_gpu_job_override
    assert not any(m("/gpu/jobs/j-1") for m, _ in agent_main._POST_ROUTES)


# -- POST /gpu/jobs/{id}/override (T649) ------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _guard():
    _brokerdb.requires_db()


@pytest.fixture()
def conn():
    with _brokerdb.ScratchDB() as db:
        with db.connect() as c:
            yield c


@pytest.fixture()
def lane(conn):
    tenant = store.create_tenant(conn, "override-tenant")
    ids = [store.enqueue_broker_job(conn, tenant["id"], "batch", {"n": i})["id"] for i in range(4)]
    return tenant, ids


def test_pin_moves_a_queued_job_to_the_head(conn, lane):
    _, ids = lane
    status, payload = gpuroutes.job_override(store, conn, ids[3], "pin")
    assert status == 200
    assert [j["job_id"] for j in payload["jobs_lane"]][0] == ids[3]


def test_reorder_places_a_job_at_a_position(conn, lane):
    _, ids = lane
    status, payload = gpuroutes.job_override(store, conn, ids[0], "reorder", position=3)
    assert status == 200
    assert [j["job_id"] for j in payload["jobs_lane"]] == [ids[1], ids[2], ids[0], ids[3]]


def test_pause_parks_a_job_at_the_tail_and_resume_returns_it_to_the_head(conn, lane):
    """Pause is expressed as lane position rather than a second state: a distinct `paused` state
    would need its own recovery, override, and metering rules for no behaviour the lane cannot
    already express."""
    _, ids = lane
    gpuroutes.job_override(store, conn, ids[0], "pause")
    assert [j["id"] for j in store.list_queued_broker_jobs(conn)][-1] == ids[0]
    gpuroutes.job_override(store, conn, ids[0], "resume")
    assert [j["id"] for j in store.list_queued_broker_jobs(conn)][0] == ids[0]


def test_a_running_job_is_never_reordered_and_says_so(conn, lane):
    _, ids = lane
    store.start_broker_job(conn, ids[0])
    for action in ("pin", "reorder", "pause", "resume"):
        status, payload = gpuroutes.job_override(store, conn, ids[0], action, position=1)
        assert status == 409, f"{action} touched a running job"
        assert "never preempted" in payload["error"]
    assert store.get_broker_job(conn, ids[0])["state"] == "running"


def test_a_running_job_can_still_be_cancelled(conn, lane):
    """Cancel is not preemption-by-another-tenant: it is the job's owner ending their own work."""
    _, ids = lane
    store.start_broker_job(conn, ids[0])
    status, _ = gpuroutes.job_override(store, conn, ids[0], "cancel")
    assert status == 200 and store.get_broker_job(conn, ids[0])["state"] == "cancelled"


def test_an_unknown_job_is_404_and_an_unknown_action_is_400(conn, lane):
    _, ids = lane
    assert gpuroutes.job_override(store, conn, "nope", "pin")[0] == 404
    assert gpuroutes.job_override(store, conn, ids[0], "explode")[0] == 400


def test_overriding_compacts_the_lane_to_contiguous_positions(conn, lane):
    _, ids = lane
    gpuroutes.job_override(store, conn, ids[2], "pin")
    positions = [j["queue_pos"] for j in store.list_queued_broker_jobs(conn)]
    assert positions == [1, 2, 3, 4], "the lane stays contiguous, so `pos` means what it says"
