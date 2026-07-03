"""In-process single-slot GPU admission (018 US2, FR-168 — the lease, realized as a lock).

The cross-process lockfile protocol existed only because the GPU tenants were separate
processes; with one owner, admission is a decision under ONE re-entrant lock — race-free by
construction, no time-of-check/time-of-use window (the free-VRAM read, the holder check, and
the claim all happen while holding `_lock`). The same lock is what makes swap transactional
(`hostagent.swap` holds it across evict→free→load, FR-171).

Live-VRAM admission (FR-175): `GpuReader` prefers NVML (`pynvml`, in-process, microseconds) and
falls back to one `nvidia-smi` fork only when NVML is unavailable; reads are cached with a short
TTL so steady-state health polling forks nothing. When the GPU is unreadable the static budget
check applies — never fail-open into co-residency.

Migration interop (FR-166): while any legacy daemon remains, every agent claim also acquires the
legacy file lease (`serving/gpu_lease.py`) under the tenant's legacy identity, so an agent
tenant and a legacy tenant can never co-reside. The shim (`lease=` seam) is deleted at T364.
"""
import logging
import threading
import time

from platformlib.topology import Tenant

_log = logging.getLogger("hostagent.admission")

#: Agent tenant id → legacy lockfile identity (the llama supervisor still claims "llm-serving").
LEGACY_TENANT = {Tenant.LLM: Tenant.LLM_LEGACY, Tenant.ASR: Tenant.ASR,
                 Tenant.VISION: Tenant.VISION, Tenant.TRAINING: Tenant.TRAINING}

_BUDGET_SAFETY = 0.95  # static-budget fallback margin — same value the lockfile lease used


class Held(Exception):
    """The single GPU slot is held by another tenant (maps to the existing 409)."""

    def __init__(self, holder: dict):
        self.holder = dict(holder or {})
        tenant = self.holder.get("tenant", "another tenant")
        if self.holder.get("kind") == "swap-reservation":
            super().__init__(f"GPU busy: a swap transaction for {tenant} is in flight "
                             f"(one GPU tenant, Principle II)")
        else:
            super().__init__(f"GPU busy: {tenant} holds the slot (one GPU tenant, Principle II)")


class VramExceeded(Exception):
    """The requested load would exceed live free VRAM / the static budget (maps to 507)."""


class GpuReader:
    """TTL-cached free-VRAM reads (FR-175). NVML in-process when available; else one
    `nvidia-smi` fork per cache miss; None when the GPU is unreadable."""

    def __init__(self, ttl_s: float = 1.0, clock=time.monotonic, read_fn=None):
        self._ttl = ttl_s
        self._clock = clock
        self._read = read_fn or self._read_gpu
        self._cached_at = None
        self._cached = None
        self._lock = threading.Lock()

    def free_gb(self, fresh: bool = False):
        with self._lock:
            now = self._clock()
            if fresh or self._cached_at is None or (now - self._cached_at) > self._ttl:
                self._cached = self._read()
                self._cached_at = now
            return self._cached

    @staticmethod
    def _read_gpu():
        try:
            import pynvml

            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                return pynvml.nvmlDeviceGetMemoryInfo(handle).free / (1024 ** 3)
            finally:
                pynvml.nvmlShutdown()
        except Exception:
            pass
        try:  # fallback — the pre-018 path, one fork per cache miss instead of per call
            import subprocess

            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return float(out.stdout.strip().splitlines()[0]) / 1024.0
        except Exception:
            pass
        return None


class Admission:
    """The single slot. All mutation happens under `lock` (re-entrant so `hostagent.swap` can
    hold it across a whole evict→free→load transaction)."""

    def __init__(self, vram_budget_gb: float, gpu: GpuReader = None, lease=None,
                 clock=time.time):
        self.lock = threading.RLock()
        self.gpu = gpu or GpuReader()
        self._lease = lease  # legacy lockfile module (interop shim) or None once retired
        self._clock = clock
        self._budget = vram_budget_gb
        self._holder = None  # {"tenant","kind","est_gb","child_pid","acquired_at"}
        self._swap_target = None  # tenant a swap transaction has reserved the slot for (FR-171)

    # -- swap reservation (FR-171) ---------------------------------------------------------------
    # The evict→free→load transaction must leave no window where a third tenant can be admitted.
    # Holding `self.lock` across the whole transaction (the first design) deadlocked ABBA with
    # every path that takes an engine's runtime lock before this one (ensure_loaded, the reaper's
    # idle_reap → release) — internal review, 018. Instead the swap RESERVES the slot: while a
    # reservation is up, acquire() admits only the reserved target; the swap itself never holds
    # this lock across engine operations, so no lock-order cycle exists.
    def begin_swap(self, target: str) -> None:
        """Reserve the slot for `target` for the duration of a swap. Raises Held if another swap
        is already in flight (one transaction at a time)."""
        with self.lock:
            if self._swap_target is not None and self._swap_target != target:
                raise Held({"tenant": self._swap_target, "kind": "swap-reservation"})
            self._swap_target = target

    def end_swap(self, target: str) -> None:
        with self.lock:
            if self._swap_target == target:
                self._swap_target = None

    def retarget_swap(self, old: str, new: str) -> None:
        """Atomically re-point an in-flight reservation (Codex round 6, 018): the ROLLBACK path
        — target failed to load after the holder was evicted — must hand the freed window to the
        evicted holder without ever dropping it, or a contender could snipe the slot in the gap
        between end_swap and the holder's re-acquire (the exact window swaps exist to close)."""
        with self.lock:
            if self._swap_target == old:
                self._swap_target = new

    # -- queries (stale-tolerant, display + gating) --------------------------------------------
    def holder(self):
        with self.lock:
            return dict(self._holder) if self._holder else None

    def free_gb(self, fresh: bool = False):
        return self.gpu.free_gb(fresh=fresh)

    # -- the admission decision (FR-168) --------------------------------------------------------
    def acquire(self, tenant: str, kind: str, est_gb: float) -> dict:
        """Claim the slot or raise. Idempotent for the current tenant (re-affirm, no re-check —
        re-running VRAM admission while your own model is resident would see low free VRAM and
        wrongly evict you; same rationale as the lockfile lease's same-tenant fast path)."""
        with self.lock:
            if self._holder is not None:
                if self._holder["tenant"] == tenant:
                    return dict(self._holder)
                raise Held(self._holder)
            if self._swap_target is not None and self._swap_target != tenant:
                # a swap transaction owns the freed slot — only its target may claim it (FR-171)
                raise Held({"tenant": self._swap_target, "kind": "swap-reservation"})
            free = self.gpu.free_gb(fresh=True)  # a fresh read only for a real admission
            if free is not None:
                if est_gb > free:
                    raise VramExceeded(
                        f"{tenant} needs ~{est_gb:.1f}GB but only {free:.1f}GB VRAM is free")
            elif est_gb > self._budget * _BUDGET_SAFETY:
                raise VramExceeded(
                    f"{tenant} needs ~{est_gb:.1f}GB, over the {self._budget:.0f}GB budget "
                    f"(GPU unreadable — static check)")
            if self._lease is not None:  # migration interop: also claim the legacy file lease
                try:
                    self._lease.acquire(LEGACY_TENANT.get(tenant, tenant), est_gb=est_gb,
                                        vram_budget_gb=self._budget)
                except Exception as e:
                    # Map the legacy lease's refusals onto OUR types (internal review, 018): the
                    # lease module is loaded separately, so its LeaseHeld/VramExceeded are foreign
                    # classes — unmapped they'd surface as a 500 instead of the 409/507 the agent
                    # surface derives from Held/VramExceeded. Matching by name keeps this module
                    # free of a hard import on the (retiring, T364) serving/gpu_lease.
                    name = type(e).__name__
                    if name == "LeaseHeld":
                        raise Held(getattr(e, "holder", None) or {}) from e
                    if name == "VramExceeded":
                        raise VramExceeded(str(e)) from e
                    raise
            self._holder = {"tenant": tenant, "kind": kind, "est_gb": est_gb,
                            "child_pid": None, "acquired_at": self._clock()}
            return dict(self._holder)

    def set_child(self, tenant: str, pid: int) -> None:
        """Record the VRAM-owning child (crash-forensics + wedge reporting; with in-process
        admission the child's liveness no longer gates the slot — children die with the agent)."""
        with self.lock:
            if self._holder and self._holder["tenant"] == tenant:
                self._holder["child_pid"] = pid
                if self._lease is not None:
                    try:
                        self._lease.set_vram_owner(LEGACY_TENANT.get(tenant, tenant), pid)
                    except Exception:
                        pass  # interop bookkeeping only — never fail a load over it

    def release(self, tenant: str) -> None:
        """Drop our claim (idempotent, own-tenant only)."""
        with self.lock:
            if self._holder and self._holder["tenant"] == tenant:
                self._holder = None
                if self._lease is not None:
                    try:
                        self._lease.release(LEGACY_TENANT.get(tenant, tenant))
                    except Exception:
                        # Never fail the in-process release over the interop shim — but do NOT
                        # swallow it silently: a dropped lease release leaves a stale lockfile
                        # record that the lease's same-owner self-heal (019/US2) must reconcile on
                        # the next acquire. Log it so the skipped release is observable, not a
                        # silent wedge (019/US2, FR-190).
                        _log.warning("legacy lease release for %s failed; the lockfile record will "
                                     "be self-healed on the next acquire", tenant, exc_info=True)
