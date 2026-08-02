"""Shape-lane scheduler over the coordinator (026 T646–T652, T680).

Two lanes with different **shapes**, not different priorities:

  * **`inference_lane`** — interleaved, admitted against the VRAM budget as it fits. Many requests
    share one resident child; there is no ordering to enforce beyond admission itself.
  * **`jobs_lane`** — strict FIFO of *exclusive* claims, persisted in Postgres so the order survives
    a host-agent restart.

The host-agent coordinator is the **sole GPU-ordering authority**: every path that can occupy the GPU
enters through `admit_serving` or `admit_job`, and this module is the thing that decides *when* a
queued job gets to call the latter. `gateway/app/scheduler.py` is NOT a competing authority — it is
018's `PolicyScheduler`, a drift/quality monitoring tick loop with no lane ordering, no VRAM
admission, and no cross-tenant queue. It *reacts* to contention rather than arbitrating it, and it is
kept as-is (T650).

## Anti-starvation (T648)

Favouring inference is right — a queued job waiting behind a burst of chat requests is the normal,
desirable case — but favouring it *without bound* means a job may never start on a busy broker. The
v1 design emitted a "starved" warning, which observes the problem rather than preventing it.

Instead: after a configurable inference **burst count** OR a head-job **wait bound**, whichever comes
first, the scheduler enters **job-drain mode** and stops admitting *new* inference. Running requests
finish — nothing is ever preempted — and the head job acquires the GPU as they drain. The bound is on
the *wait*, so the guarantee a tenant gets is a time, not a probability.
"""
import logging
import os
import threading
import time

from hostagent import coordinator as co

logger = logging.getLogger("hostagent.scheduler")

#: How many inference admissions may be granted while a job waits, before job-drain mode engages.
DEFAULT_INFERENCE_BURST = int(os.getenv("BROKER_INFERENCE_BURST", "64"))
#: How long the head job may wait before job-drain mode engages regardless of the burst count.
DEFAULT_HEAD_JOB_WAIT_S = float(os.getenv("BROKER_HEAD_JOB_WAIT_S", "60"))


class SystemTenant:
    """The reserved tenant policy-triggered retrains run as (T651).

    `PolicyScheduler` calls the agent's `/train` directly today — the real single-authority gap,
    since that path enters no lane at all. Routing those retrains here means they queue FIFO by
    arrival with no privileged bypass, honour the single `exclusive_job` slot, and have their
    GPU-seconds metered to a real tenant rather than being invisible.
    """

    NAME = "system"


class Scheduler:
    """Feeds the coordinator from the two lanes and enforces the anti-starvation bound."""

    def __init__(self, coordinator, store=None, conn_factory=None, *,
                 inference_burst=None, head_job_wait_s=None, clock=time.monotonic):
        self.coordinator = coordinator
        self._store = store
        self._conn_factory = conn_factory
        self._clock = clock
        self.inference_burst = (DEFAULT_INFERENCE_BURST if inference_burst is None
                                else inference_burst)
        self.head_job_wait_s = (DEFAULT_HEAD_JOB_WAIT_S if head_job_wait_s is None
                                else head_job_wait_s)

        self._lock = threading.Lock()
        self._inference_since_job_queued = 0
        self._head_job_since = None   # when the current head job started waiting
        self._drain_mode = False
        self._queued_ids = []         # mirror of the persisted lane, for the in-memory fast path

    # -- lane state ---------------------------------------------------------------------------------

    def _conn(self):
        if self._conn_factory is None:
            return None
        return self._conn_factory()

    def queued(self) -> list:
        conn = self._conn()
        if conn is None or self._store is None:
            return list(self._queued_ids)
        return [j["id"] for j in self._store.list_queued(conn)]

    def note_job_queued(self, job_id: str = None) -> None:
        """A job entered the lane. Starts the head-job wait clock if this is now the head."""
        with self._lock:
            if job_id is not None and job_id not in self._queued_ids:
                self._queued_ids.append(job_id)
            if self._head_job_since is None:
                self._head_job_since = self._clock()
                self._inference_since_job_queued = 0

    def note_job_started(self, job_id: str = None) -> None:
        """The head job acquired the GPU: the wait clock and the drain mode both reset."""
        with self._lock:
            if job_id is not None and job_id in self._queued_ids:
                self._queued_ids.remove(job_id)
            self._head_job_since = self._clock() if self._queued_ids else None
            self._inference_since_job_queued = 0
            self._drain_mode = False

    # -- job-drain mode -------------------------------------------------------------------------------

    def drain_mode(self) -> bool:
        """True when new inference must stop so the head job can acquire the GPU."""
        with self._lock:
            return self._compute_drain_mode()

    def _compute_drain_mode(self) -> bool:
        if self._head_job_since is None:
            self._drain_mode = False
            return False
        waited = self._clock() - self._head_job_since
        if (self._inference_since_job_queued >= self.inference_burst
                or waited >= self.head_job_wait_s):
            if not self._drain_mode:
                logger.info("job-drain mode: head job waited %.1fs after %d inference admissions",
                            waited, self._inference_since_job_queued)
            self._drain_mode = True
        return self._drain_mode

    # -- admission entry points -------------------------------------------------------------------------

    def admit_serving(self, model_key, est_bytes, *, op_id=None, deadline=None):
        """Admit an inference request unless job-drain mode is engaged.

        A refusal here is `gpu_busy` — transient by construction, since the head job will acquire the
        GPU and release it. Running requests are never affected: drain mode stops *new* admissions
        only, and preempting an in-flight request is not something any path in this design does.
        """
        if self.drain_mode():
            return co.Refuse(co.GPU_BUSY,
                             "job-drain mode: a queued job is waiting for the GPU",
                             retry_after=2.0)
        result = self.coordinator.admit_serving(model_key, est_bytes, op_id=op_id,
                                                deadline=deadline)
        if result.ok:
            with self._lock:
                if self._head_job_since is not None:
                    self._inference_since_job_queued += 1
        return result

    def admit_head_job(self, job_id, deadline=None) -> bool:
        """Try to give the head job the GPU. Returns True when it acquired it."""
        acquired = self.coordinator.admit_job(job_id, deadline=deadline)
        if acquired:
            self.note_job_started(job_id)
        return acquired

    def end_job(self, job_id=None) -> None:
        self.coordinator.end_job(job_id)
        with self._lock:
            self._head_job_since = self._clock() if self._queued_ids else None
            self._inference_since_job_queued = 0
            self._drain_mode = False

    # -- observability ------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        """The coordinator's VRAM/residency view plus the lane state — the payload behind
        `GET /admin/queue` (T689)."""
        snap = self.coordinator.snapshot()
        conn = self._conn()
        jobs_lane = []
        if conn is not None and self._store is not None:
            try:
                jobs_lane = [{"job_id": j["id"], "tenant_id": j["tenant_id"], "pos": j["queue_pos"],
                              "kind": j["kind"]} for j in self._store.list_queued(conn)]
            except Exception:  # noqa: BLE001 — a store blip degrades the view, never the scheduler
                jobs_lane = []
        snap["jobs_lane"] = jobs_lane
        drain = self.drain_mode()
        snap["inference_lane"] = {"drain_mode": drain,
                                  "admissions_since_job_queued": self._inference_since_job_queued}
        try:  # T670: lane depth and drain mode, best-effort like the coordinator's own gauges
            from hostagent.metrics import REGISTRY

            REGISTRY.set_gauge("hostagent_jobs_lane_depth", len(jobs_lane))
            REGISTRY.set_gauge("hostagent_job_drain_mode", 1 if drain else 0)
        except Exception:  # noqa: BLE001
            pass
        return snap
