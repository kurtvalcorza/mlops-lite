"""Shared tenant lifecycle + the engine-adapter interface (018 US2, FR-170).

ONE implementation of load-on-demand → ready → drain → idle-release → unload → stuck-child reap
for every engine; per-engine specifics live in an adapter. This is the consolidation that ends
the llama/whisper copy-drift (review §4.2): a lifecycle fix lands here once.

Adapter interface (duck-typed; data-model.md §EngineAdapter):
    engine_id: str            gpu: bool            optional: bool
    available() -> (ok, reason)          # binary/model prerequisites present?
    estimate_vram() -> float             # admission estimate (GB); 0 for CPU engines
    spawn() -> child                     # Popen-like: .pid .poll() .terminate() .kill() .wait(t)
    ready() -> bool                      # readiness probe against the spawned child

States (data-model.md §Tenant): disabled | unavailable | cold | loading | ready | draining |
idle-releasing | unloaded(→cold) | wedged. `wedged` (kill failed — uninterruptible child) keeps
the admission slot occupied and is surfaced on health/metrics instead of silently pinning the
GPU (spec edge case).
"""
import threading
import time

from . import admission as adm


class EngineError(Exception):
    """A lifecycle operation failed (spawn/readiness) — maps to 502 at the surface."""


class EngineRuntime:
    """One engine's live state under the shared lifecycle. GPU work is serialized per-engine by
    `self.lock`; the admission decision itself is `admission.lock` (the platform-wide slot)."""

    def __init__(self, adapter, admission: adm.Admission, *, kind: str = "serving",
                 idle_timeout_s: float = 120.0, ready_wait_s: float = 60.0,
                 clock=time.monotonic, sleep=time.sleep, enabled: bool = True):
        self.adapter = adapter
        self.admission = admission
        self.kind = kind
        self.idle_timeout_s = idle_timeout_s
        self.ready_wait_s = ready_wait_s
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.RLock()
        self.child = None
        self.last_used = None
        self.wedged_reason = None
        self.enabled = enabled

    # -- state reporting -------------------------------------------------------------------------
    def state(self) -> dict:
        """Display-only, deliberately LOCK-FREE (Codex round 2, 018): /health and /metrics call
        this for every engine, and taking `self.lock` would block the read surface behind a cold
        load or a long in-flight request — probes would mark the agent down exactly while an
        engine is busy. A momentarily stale answer is fine for display (the same stale-tolerant
        stance as the legacy lease's `current_holder`); admission decisions never read this."""
        if not self.enabled:
            return {"state": "disabled"}
        if self.wedged_reason:
            return {"state": "wedged", "reason": self.wedged_reason}
        ok, reason = self.adapter.available()
        if not ok:
            return {"state": "unavailable", "reason": reason}
        child = self.child  # local ref — the field may flip mid-read (lock-free by design)
        if child is not None and child.poll() is None:
            return {"state": "ready" if self.adapter.ready() else "loading"}
        return {"state": "cold"}

    def _resident(self) -> bool:
        return self.child is not None and self.child.poll() is None

    # -- the shared load path (FR-170; includes FR-167's reap, structural for every engine) ------
    def ensure_loaded(self) -> float:
        """Load on demand; returns cold-start ms (0.0 when already resident+ready). Raises
        `admission.Held`/`VramExceeded` (409/507) or `EngineError` (502/503)."""
        with self.lock:
            if not self.enabled:
                raise EngineError(f"{self.adapter.engine_id} is disabled")
            if self.wedged_reason:
                raise EngineError(f"{self.adapter.engine_id} is wedged: {self.wedged_reason}")
            ok, reason = self.adapter.available()
            if not ok:
                raise EngineError(f"{self.adapter.engine_id} unavailable: {reason}")
            if self._resident() and self.adapter.ready():
                self.last_used = self.clock()
                return 0.0
            # Resident but NOT ready → reap before relaunch, uniformly (FR-167/T351, now shared).
            if self._resident():
                self.unload(drain_timeout_s=0)
                if self.wedged_reason:
                    raise EngineError(f"{self.adapter.engine_id} is wedged: {self.wedged_reason}")
            if self.adapter.gpu:
                self.admission.acquire(self.adapter.engine_id, self.kind,
                                       self.adapter.estimate_vram())
            # CPU engines are admission-exempt (Principle II) but share the SAME failed-load
            # cleanup (Codex review, 018): without it a never-ready CPU child stayed resident
            # with last_used unset — invisible to the idle reaper, wedging its port until restart.
            try:
                return self._spawn_and_wait()
            except BaseException:
                self.unload(drain_timeout_s=0)  # never hold the slot/child after a failed load
                raise

    def _spawn_and_wait(self) -> float:
        t0 = self.clock()
        self.child = self.adapter.spawn()
        if self.adapter.gpu:
            self.admission.set_child(self.adapter.engine_id, self.child.pid)
        waited = 0.0
        while waited <= self.ready_wait_s:
            if self.adapter.ready():
                self.last_used = self.clock()
                return round((self.clock() - t0) * 1000, 1)
            if self.child.poll() is not None:
                raise EngineError(f"{self.adapter.engine_id} child exited during startup")
            self.sleep(0.5)
            waited += 0.5
        raise EngineError(f"{self.adapter.engine_id} did not become ready in {self.ready_wait_s}s")

    def touch(self) -> None:
        with self.lock:
            self.last_used = self.clock()

    # -- unload / drain / wedge (FR-170; wedge semantics per spec edge case) ---------------------
    def unload(self, drain_timeout_s: float = 10.0) -> dict:
        """Drain (bounded: try to take `self.lock` for up to `drain_timeout_s`), terminate the
        child, wait for teardown, release admission. A child that survives SIGKILL leaves the
        engine `wedged` WITH the slot still held — the GPU is not actually free.

        On drain timeout the teardown proceeds WITHOUT the runtime lock — the llama supervisor's
        hard-cut semantics (Codex review, 018): a wedged in-flight request must not let unload()
        block past the advertised drain bound (a swap or reap would hang forever). The in-flight
        request loses its child; the lock-free mutation window is the same one the legacy
        `_unload_now` accepted."""
        acquired = self.lock.acquire(timeout=drain_timeout_s) if drain_timeout_s > 0 else \
            self.lock.acquire(blocking=False)
        try:
            return self._teardown(drained=bool(acquired))
        finally:
            if acquired:
                self.lock.release()

    def _teardown(self, drained: bool) -> dict:
        """The terminate→kill→wedge→release sequence. Caller holds `self.lock` on a clean drain;
        on a hard cut it runs lock-free (documented above)."""
        if not self._resident():
            self.child = None
            if self.adapter.gpu and not self.wedged_reason:
                self.admission.release(self.adapter.engine_id)
            return {"status": "idle", "drained": drained}
        child = self.child
        child.terminate()
        if not self._wait(child, 10):
            child.kill()
            if not self._wait(child, 10):
                self.wedged_reason = (f"child pid={child.pid} survived SIGKILL "
                                      f"(uninterruptible) — GPU slot NOT released")
                return {"status": "busy", "drained": drained,
                        "detail": self.wedged_reason}
        self.child = None
        self.wedged_reason = None
        if self.adapter.gpu:
            self.admission.release(self.adapter.engine_id)
        return {"status": "unloaded", "drained": drained}

    @staticmethod
    def _wait(child, timeout_s: float) -> bool:
        try:
            child.wait(timeout_s)
            return True
        except Exception:
            return child.poll() is not None

    def idle_reap(self, now: float = None) -> bool:
        """One tick of the shared idle-reaper: unload when resident and idle past the timeout."""
        with self.lock:
            if not self._resident() or self.last_used is None:
                return False
            now = self.clock() if now is None else now
            if (now - self.last_used) > self.idle_timeout_s:
                self.unload(drain_timeout_s=0)
                return True
            return False


class EngineManager:
    """The agent's engine table + the ONE idle-reaper thread (replacing five polling loops)."""

    def __init__(self, admission: adm.Admission, runtimes: dict, poll_interval_s: float = 5.0):
        self.admission = admission
        self.runtimes = dict(runtimes)  # engine_id -> EngineRuntime
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()

    def engine_states(self) -> dict:
        return {eid: rt.state() for eid, rt in self.runtimes.items()}

    def reaper_tick(self) -> None:
        for rt in self.runtimes.values():
            try:
                rt.idle_reap()
            except Exception:
                pass  # one engine's reap failure must not starve the others

    def run_reaper(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            self.reaper_tick()

    def stop(self) -> None:
        self._stop.set()
