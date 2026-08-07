"""Coordinator-backed admission for the engine runtimes (026 T653, T654, T645).

`hostagent/lifecycle.py`'s runtimes call `admission.acquire(engine_id, kind, est_gb)` — the
single-slot lease's surface. This shim presents exactly that surface over the coordinator, so
**multiple engine children can be co-resident** without `lifecycle.py`, `swap.py`, or `jobs.py`
changing at all. That is the point: the redesign is in the admission core, and the layers above it
were already written against an interface, so they should not have to learn a new one.

## The mapping, and where it is deliberately lossy

| legacy call | coordinator |
|---|---|
| `acquire(id, "serving", est)` | `admit_serving(id, est)` → resident; the load claim is released at once |
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

#: Marks an engine as RESIDENT in the shim's own map, holding no coordinator claim.
#:
#: Distinct from `None`, which the map already uses for an exclusive job. Three states, three
#: values: absent (not ours), `_RESIDENT` (a serving engine we admitted), `None` (a job holding the
#: whole GPU).
_RESIDENT = object()


def enabled() -> bool:
    return os.getenv("BROKER_COORDINATOR_ADMISSION", "0").lower() in ("1", "true", "yes", "on")


class RuntimeLifecycle:
    """The coordinator's lifecycle when it is driven by `hostagent/lifecycle.py`'s engine runtimes.

    **The runtime owns loading; the coordinator owns accounting.** That is forced by the call order
    the legacy surface imposes:

        runtime: admission.acquire(...)   ← the coordinator admits, and would like to load
        runtime: child = adapter.spawn()  ← the REAL load happens here
        runtime: admission.set_child(...) ← and only now is the real PID known

    So the coordinator cannot be the thing that loads: by the time it is asked to admit, the process
    it would measure does not exist yet. This lifecycle therefore returns a **deferred** child whose
    PID is `None`, meaning "the caller is spawning it; reconcile when it reports back".

    Leaving `NullLifecycle` wired here — which is what production did — was not merely a stub. Its
    `load()` returns a fake child with `pid=0`, so stage 3 reconciled the reservation against PID 0,
    measuring some other process or nothing at all; `set_child()` then declined to install the real
    PID because a child was already present; and eviction called a no-op `unload()`, leaving the real
    engine running while the coordinator recorded it as gone. Production and the co-residency tests
    were describing two different systems.

    Unloading routes back to the real runtime, because there is exactly one thing that can stop that
    child and it is not this module.
    """

    def __init__(self, drain_timeout_s: float = 10.0):
        #: engine_id -> the `EngineRuntime` that owns that engine's child.
        self._runtimes = {}
        self._drain_timeout_s = drain_timeout_s

    def register(self, engine_id: str, runtime) -> None:
        self._runtimes[engine_id] = runtime

    def load(self, model_key):
        """A deferred child: admitted, not yet spawned.

        `pid=None` is deliberate and is read by `GpuProbe.used_by_pid`, which returns 0 for it — so
        the reservation keeps its **estimate** until the real PID arrives, rather than reconciling
        to a measurement of the wrong process. An estimate held a moment longer is conservative; a
        confident measurement of PID 0 is not.
        """
        return type("DeferredChild", (), {"pid": None, "model_key": model_key})()

    def unload(self, model_key, child=None):
        runtime = self._runtimes.get(model_key)
        if runtime is None:
            # Nothing registered for this key. Raising would turn an eviction into an admission
            # failure; the coordinator's caller cannot act on it either way, and the accounting drop
            # is still correct. Logged by the coordinator's own eviction path.
            return None
        # A real drain budget, not a hard cut. The coordinator has already decided this model
        # should go; the runtime is the layer that knows whether a request is in flight, and its
        # lock is what inference holds. Passing 0 would cut a live request mid-response, which is
        # exactly what `evict()`'s drain phase exists to avoid — and with the shim no longer
        # holding a permanent claim, the coordinator's own `active_requests` can no longer do the
        # waiting on its behalf.
        return runtime.unload(drain_timeout_s=self._drain_timeout_s)


class CoordinatorAdmission:
    """The legacy `Admission` surface, served by the coordinator."""

    def __init__(self, coordinator, clock=None):
        self.coordinator = coordinator
        self._clock = clock or __import__("time").time
        #: engine_id -> `_RESIDENT` for a serving engine, `None` for an exclusive job.
        #:
        #: **No long-lived claim.** An earlier version held one coordinator claim per engine for the
        #: whole resident lifetime, which kept `active_requests` above zero and deadlocked eviction:
        #: `evict()` waits for zero before calling the unload that would have released it. Residency
        #: lives in the coordinator's resident entry; request lifetime lives in the runtime, whose
        #: lock inference actually holds.
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

        # **Release the load claim immediately.** Residency is the resident entry; the claim is a
        # per-REQUEST reference count, and holding one for the whole resident lifetime deadlocked
        # eviction outright: `Coordinator.evict()` waits for `active_requests == 0` before calling
        # the unload that would have released the claim keeping it above zero. Capacity-pressure
        # eviction and exclusive-job drain could therefore never complete in production — only in
        # tests, which released the claim by hand first.
        #
        # Draining real in-flight work still happens, in the layer that knows about it: eviction
        # calls back through `RuntimeLifecycle.unload` into `EngineRuntime.unload`, which drains
        # under the runtime lock that inference actually holds. The coordinator owns residency
        # accounting; the runtime owns request lifetime. Splitting them this way is what the two
        # were always separately about.
        result.claim.release()
        with self._lock:
            self._claims[tenant] = _RESIDENT
        return self._holder_dict(tenant, kind, est_gb)

    def release(self, tenant: str) -> None:
        """Drop this engine's claim (idempotent, own-tenant only), and forget the resident **iff the
        engine that owns it is actually gone**.

        `release(engine_id)` carries two different meanings depending on who calls it, and the
        difference matters enormously:

          * **idle** — the engine has no in-flight work. The model stays resident so the next
            request finds it warm, and the claim drop is precisely what makes it *evictable*. This
            is co-residency working as designed; forgetting here would throw away a loaded model's
            accounting while its process is still holding the VRAM.
          * **gone** — `EngineRuntime._teardown` calls this after killing the child. The model is
            off the GPU, and keeping it accounted permanently shrinks the usable budget for a
            process that no longer exists. Every idle reap and operator unload did exactly that.

        The caller cannot tell us which it meant — the legacy surface has one verb — so this asks
        the registered runtime instead: a runtime whose `child` is `None` has no process. That is a
        fact about the world rather than an inference about intent, which is the only basis on which
        these two cases can be told apart.

        With no runtime registered (the shim driven directly, as the co-residency suites do) the
        resident is kept, because nothing has claimed the child is dead.
        """
        with self._lock:
            claim = self._claims.pop(tenant, "absent")
        if claim == "absent":
            return
        if claim is None:
            self.coordinator.end_job(tenant)
            return
        if claim is not _RESIDENT:
            claim.release()   # a real claim (legacy path); residency markers hold none

        lifecycle = getattr(self.coordinator.lifecycle, "inner", self.coordinator.lifecycle)
        runtime = getattr(lifecycle, "_runtimes", {}).get(tenant)
        if runtime is not None and getattr(runtime, "child", None) is None:
            # `forget()` rather than `evict()`: the child is already stopped, and evicting would
            # drain and unload something gone. The coordinator's own eviction path removes the entry
            # before reaching here, so that case is a harmless no-op.
            self.coordinator.forget(tenant)

    def set_child(self, tenant: str, pid: int) -> None:
        """Install the **real** spawned PID and reconcile the accounted VRAM against it.

        Two bugs lived in the old three-line version. It only assigned when `entry.child` was
        `None`, but the lifecycle had already put a placeholder there — so the real PID was silently
        discarded and every later per-PID reading measured a process that does not exist. And it
        never re-measured, so the resident's accounted bytes stayed at whatever stage 3 recorded for
        the placeholder, which is the number both VRAM bounds are then enforced against.

        Re-measuring here is the whole point of the callback: this is the first moment the platform
        knows which process holds the model's memory.
        """
        measured = None
        try:
            measured = self.coordinator.gpu.used_by_pid(pid)
        except Exception:  # noqa: BLE001 — an unreadable probe leaves the estimate in place
            measured = None

        with self.coordinator._locked():
            entry = self.coordinator.residents.get(tenant)
            if entry is None:
                return
            # Always replace: the placeholder is not a child, and keeping it would mean the
            # coordinator can never see the process it is accounting for.
            entry.child = type("Child", (), {"pid": pid})()
            if measured:
                # Only when the probe returned something. A zero reading is far more likely to mean
                # "the process has not allocated yet" than "this model is free", and adopting it
                # would drop the model out of the budget entirely.
                entry.vram_accounted_bytes = measured
                # The bytes are now real and visible in live-free, so invariant 2 must stop
                # deducting them — continuing would double-count this model against itself and
                # refuse admissions that genuinely fit. An unreadable probe leaves the flag as it
                # was: still deducting an estimate is conservative, which is the safe direction.
                entry.materialized = True

    def holder(self):
        """The exclusive job if one runs, else the most recently used resident — see the module
        docstring on why this is lossy and why that is the right loss."""
        snap = self.coordinator.snapshot()
        if snap["active_job"]:
            return {"tenant": snap["active_job"]["job_id"], "kind": "job", "est_gb": None,
                    "child_pid": None, "acquired_at": snap["active_job"]["started_at"]}
        # `recency_seq` breaks ties `last_used_at` cannot: a coarse wallclock (15.625 ms on Windows)
        # puts two acquires in one tick, and without it the "most recent" holder is whichever the
        # resident dict happened to hold first. Defaulted for any snapshot predating the field.
        residents = sorted(snap["resident"],
                           key=lambda r: (r["last_used_at"], r.get("recency_seq", 0)), reverse=True)
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
