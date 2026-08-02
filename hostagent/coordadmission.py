"""Coordinator-backed admission for the engine runtimes (026 T653, T654, T645).

`hostagent/lifecycle.py`'s runtimes call `admission.acquire(engine_id, kind, est_gb)` — the
single-slot lease's surface. This shim presents exactly that surface over the coordinator, so
**multiple engine children can be co-resident** without `lifecycle.py`, `swap.py`, or `jobs.py`
changing at all. That is the point: the redesign is in the admission core, and the layers above it
were already written against an interface, so they should not have to learn a new one.

## The mapping, and where it is deliberately lossy

| legacy call | coordinator |
|---|---|
| `acquire(id, "serving", est)` | `admit_serving(id, est)` → a claim held on the caller's behalf |
| `acquire(id, "job", est)` | `admit_job(id)` — whole GPU, never preempted |
| `release(id)` | release the claim / `end_job` |
| `holder()` | the exclusive job if one runs, else the most recently used resident |

`holder()` is the lossy one, and unavoidably so: with co-residency there is no longer *a* holder.
It returns the answer that keeps the pre-broker consumers (health payloads, wedge reporting, the
`Held` error message) truthful — "who most recently had the GPU" — and `snapshot()` is the honest
full answer for anything that needs it.

## Swap reservations become no-ops, on purpose

`begin_swap`/`end_swap`/`retarget_swap` exist because the single slot needed a way to hold a freed
slot across evict→load without a third tenant sniping it. The coordinator has no such window: making
room and reserving it happen inside one admission, and `admit_serving` evicts as needed. Keeping
them as accepted no-ops means `hostagent/swap.py` keeps working unmodified while its reservation
protocol becomes redundant rather than wrong.

## Why this is opt-in

`BROKER_COORDINATOR_ADMISSION` gates it, default **off**. Co-residency changes what the GPU does
under load, and 026's delivery is phase-gated: P3/P4 are verified on hardware before they become the
default. Off, the agent's behaviour is byte-identical to 018's. On, engines co-reside within the two
bounds. The flag is the phase gate made operational, not a hedge about whether the code works —
`tests/test_broker_coadmission.py` drives every path of it.
"""
import os
import threading

from hostagent import admission as adm
from hostagent import coordinator as co

_GIB = 1024 ** 3


def enabled() -> bool:
    return os.getenv("BROKER_COORDINATOR_ADMISSION", "0").lower() in ("1", "true", "yes", "on")


class CoordinatorAdmission:
    """The legacy `Admission` surface, served by the coordinator."""

    def __init__(self, coordinator, clock=None):
        self.coordinator = coordinator
        self._clock = clock or __import__("time").time
        #: engine_id -> the claim this shim holds for it. One per engine, because the legacy surface
        #: is `acquire(engine_id)` / `release(engine_id)` — a per-request claim would have nowhere to
        #: live in that vocabulary. The coordinator's ref-count still protects the child; this shim
        #: simply holds one long-lived claim while the engine is resident, and `evict` drains it.
        self._claims = {}
        self._lock = threading.RLock()

    # -- the surface `lifecycle.py` uses ------------------------------------------------------------

    @property
    def lock(self):
        """`hostagent.swap` holds this across its transaction. Handing back the coordinator's own
        lock would be the ABBA deadlock the redesign removes, so this is a SEPARATE re-entrant lock
        that serializes swap transactions among themselves and nothing else."""
        return self._lock

    def acquire(self, tenant: str, kind: str, est_gb: float) -> dict:
        """Admit `tenant` (an engine id), or raise the legacy `Held` / `VramExceeded`.

        Idempotent for an engine that already holds a claim — re-running admission against your own
        resident model would see the low free VRAM your model caused, exactly the trap the
        single-slot lease's same-tenant fast path avoided.
        """
        with self._lock:
            if tenant in self._claims:
                return self._holder_dict(tenant, kind, est_gb)

        if kind == "job":
            if not self.coordinator.admit_job(tenant):
                raise adm.Held({"tenant": self._current_holder_name() or "another tenant",
                                "kind": "job"})
            with self._lock:
                self._claims[tenant] = None  # a job holds the whole GPU, not a per-model claim
            return self._holder_dict(tenant, kind, est_gb)

        result = self.coordinator.admit_serving(tenant, est_gb * _GIB)
        if isinstance(result, co.Refuse):
            if result.code == co.MODEL_TOO_LARGE:
                raise adm.VramExceeded(result.message)
            # gpu_busy and load_failed both map to the legacy 409 vocabulary. `Held` is what the
            # pre-broker consumers already branch on, and inventing a new exception here would mean
            # every one of them growing a case for it.
            raise adm.Held({"tenant": self._current_holder_name() or "another tenant",
                            "kind": "serving"})
        with self._lock:
            self._claims[tenant] = result.claim
        return self._holder_dict(tenant, kind, est_gb)

    def release(self, tenant: str) -> None:
        """Drop this engine's claim (idempotent, own-tenant only)."""
        with self._lock:
            claim = self._claims.pop(tenant, "absent")
        if claim == "absent":
            return
        if claim is None:
            self.coordinator.end_job(tenant)
        else:
            claim.release()

    def set_child(self, tenant: str, pid: int) -> None:
        with self.coordinator._locked():
            entry = self.coordinator.residents.get(tenant)
            if entry is not None and entry.child is None:
                entry.child = type("Child", (), {"pid": pid})()

    def holder(self):
        """The exclusive job if one runs, else the most recently used resident — see the module
        docstring on why this is lossy and why that is the right loss."""
        snap = self.coordinator.snapshot()
        if snap["active_job"]:
            return {"tenant": snap["active_job"]["job_id"], "kind": "job", "est_gb": None,
                    "child_pid": None, "acquired_at": snap["active_job"]["started_at"]}
        residents = sorted(snap["resident"], key=lambda r: r["last_used_at"], reverse=True)
        if not residents:
            return None
        top = residents[0]
        return {"tenant": top["model"], "kind": "serving",
                "est_gb": top["vram_accounted_bytes"] / _GIB, "child_pid": None,
                "acquired_at": top["last_used_at"]}

    def free_gb(self, fresh: bool = False):
        try:
            return self.coordinator.gpu.free_bytes() / _GIB
        except Exception:  # noqa: BLE001 — an unreadable GPU is None, as it always was
            return None

    # -- swap reservations: accepted no-ops (see the module docstring) -------------------------------

    def begin_swap(self, target: str) -> None:
        return None

    def end_swap(self, target: str) -> None:
        return None

    def retarget_swap(self, old: str, new: str) -> None:
        return None

    # -- helpers ---------------------------------------------------------------------------------------

    def _holder_dict(self, tenant, kind, est_gb) -> dict:
        return {"tenant": tenant, "kind": kind, "est_gb": est_gb, "child_pid": None,
                "acquired_at": self._clock()}

    def _current_holder_name(self):
        holder = self.holder()
        return holder["tenant"] if holder else None

    def snapshot(self) -> dict:
        """The honest full answer `holder()` cannot give."""
        return self.coordinator.snapshot()
