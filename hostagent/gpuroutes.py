"""Broker GPU routes on the host agent (026 T689, T649, T680, T651).

Two routes, both owned by the agent because the agent is the sole GPU-ordering authority: the
gateway holds no admission state and synthesizing any there would create a second, lagging answer to
a question with exactly one authority.

  * `GET  /gpu/queue`               — resident set, reservations, both terms of both VRAM bounds,
                                      and both lanes. Proxied verbatim by `GET /admin/queue`.
  * `POST /gpu/jobs/{id}/override`  — owner pin/pause/resume/reorder/cancel over the QUEUED lane.
                                      A running job answers 409 and is never touched.

Kept in their own module rather than added to `hostagent/main.py`'s route tables so the broker's
surface is separable from the pre-broker one — `main.py` registers them, and everything the broker
needs to answer lives here.
"""
import logging

logger = logging.getLogger("hostagent.gpuroutes")

#: Bytes -> MB, the unit `contracts/admin-api.md` publishes. Kept as one helper so the endpoint and
#: its tests cannot disagree about the divisor.
_MB = 1024 ** 2


def _mb(value) -> float:
    return round((value or 0.0) / _MB, 1)


def queue_payload(scheduler) -> dict:
    """The `GET /gpu/queue` body, in `contracts/admin-api.md`'s field names and units.

    Both terms of both bounds are first-class fields, and `reserved_mb` and `unmaterialized_mb` are
    **different sums that are both needed**: invariant 1 counts every outstanding reservation against
    the budget, while invariant 2 deducts only the not-yet-reconciled ones from live-free — a
    reservation that has already materialized is by then visible in `live_free` itself, and
    subtracting it twice would refuse valid loads. `unmaterialized_mb` is derivable by summing the
    `reservations` entries, but an operator mid-drill should not have to do that arithmetic, and
    getting it wrong silently produces a *passing* assertion against the wrong quantity.
    """
    snap = scheduler.snapshot()
    vram = snap.get("vram") or {}
    return {
        "resident": [{"model": r["model"], "state": r["state"],
                      "vram_mb": _mb(r["vram_accounted_bytes"]),
                      "active_requests": r["active_requests"], "idle": r["idle"]}
                     for r in snap.get("resident", [])],
        "reservations": [{"op_id": r["op_id"], "model": r["model"],
                          "est_mb": _mb(r["est_bytes"]), "materialized": r["materialized"],
                          "waiters": r["waiters"]}
                         for r in snap.get("reservations", [])],
        "vram": {
            "usable_capacity_mb": _mb(vram.get("usable_capacity")),
            "accounted_mb": _mb(vram.get("accounted")),
            "reserved_mb": _mb(vram.get("reserved")),
            "unmaterialized_mb": _mb(vram.get("unmaterialized")),
            "live_free_mb": _mb(vram.get("live_free")),
            "safety_headroom_mb": _mb(vram.get("safety_headroom")),
        },
        "inference_lane": snap.get("inference_lane", {}),
        "jobs_lane": snap.get("jobs_lane", []),
        "active_job": snap.get("active_job"),
        "job_barrier": snap.get("job_barrier", False),
    }


def job_override(store, conn, job_id: str, action: str, position: int = None) -> tuple:
    """`(status, payload)` for an owner override. A running job is 409 on every action.

    409 rather than a silent no-op: an operator who reordered the wrong id must be told. And the
    running job is untouchable by design — FR-010/FR-023a, enforced here so the rule holds for every
    caller rather than only the one route that remembered it.
    """
    job = store.get_broker_job(conn, job_id)
    if job is None:
        return 404, {"error": "unknown job"}
    if job["state"] == "running":
        # EVERY action, including cancel. The guard used to read `and action != "cancel"`, which let
        # an override flip a running row to `cancelled` in the database while the GPU process kept
        # running — splitting the persisted lane, the coordinator's ownership, and the reservation
        # that is still accruing against the tenant. The row would say the job is over; the device
        # would disagree, and nothing would reconcile them.
        #
        # There is no runtime cancellation path for a running job, and inventing one here would mean
        # killing a child from a route that holds none of the coordinator's state. If it is ever
        # supported it needs a coordinator-controlled stop-and-settle lifecycle, not a store write.
        return 409, {"error": "a running job is never preempted, reordered, or cancelled"}

    try:
        if action == "pin":
            store.pin_broker_job(conn, job_id)
        elif action == "reorder":
            store.reorder_broker_job(conn, job_id, int(position or 1))
        elif action == "cancel":
            store.cancel_broker_job(conn, job_id)
        elif action in ("pause", "resume"):
            # Pause/resume is expressed as lane position rather than a second state: a paused job
            # parked at the tail cannot start, and it re-enters at its arrival position on resume.
            # A distinct `paused` state would need its own recovery, override, and metering rules
            # for no behaviour the lane cannot already express.
            queued = store.list_queued_broker_jobs(conn)
            store.reorder_broker_job(conn, job_id, len(queued) if action == "pause" else 1)
        else:
            return 400, {"error": f"unknown override {action!r}"}
    except store.StoreError as e:
        return 409, {"error": str(e)}

    return 200, {"job": store.get_broker_job(conn, job_id),
                 "jobs_lane": [{"job_id": j["id"], "pos": j["queue_pos"]}
                               for j in store.list_queued_broker_jobs(conn)]}
