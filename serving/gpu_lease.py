#!/usr/bin/env python3
"""Single-slot, self-healing GPU lease (008 US1, T134/T135 — FR-062/063/064/073).

The platform's NON-NEGOTIABLE rule (constitution Principle II, v1.4.0) is "exactly one GPU
tenant resident at any instant — any GPU-resident modality (LLM, vision, …) OR a training run".
008 generalizes the two cooperating HTTP guards (`supervisor.py` refuse-while-training +
`trainer.py` refuse-while-serving) into this one **atomic lease**, closing the time-of-check/
time-of-use race they had: two callers could each poll "is the other idle?", both see free, and
both proceed onto the GPU.

The three GPU users are **separate native WSL processes** (the llama-server supervisor, the
trainer, and the BentoML vision service) that share the WSL filesystem. They self-arbitrate by
acquiring this lease before any GPU work — no gateway round-trip (the Docker gateway only proxies,
never holds the GPU, and training is fire-and-forget so it can't be gated in-gateway). The lease is
**stdlib only** (no new dependency, Principle III).

Primitive (grilled 2026-06-28): a **PID-stamped lockfile** on the WSL filesystem, claimed via
`os.open(..., O_CREAT|O_EXCL)` (atomic create) and reclaimed when its recorded PID is dead
(`os.kill(pid, 0)` raises → stale → reclaim, satisfying the FR-063 self-healing requirement). The
read-decide-claim sequence is serialized by an `fcntl.flock` on a persistent sidecar so stale
reclamation cannot race (two reclaimers must not both clobber a freshly-acquired holder); the flock
also auto-releases on a holder's death, a kernel-level backstop to the os.kill check. The flock and
O_EXCL are belt-and-suspenders realizations of the same "atomic host lockfile, self-healing" intent.

Admission (FR-064): at acquire time the lease reads **live free VRAM** from `nvidia-smi`
(`--query-gpu=memory.free`) — the same read `trainer.py` already uses — and refuses a load whose
estimated footprint would exceed it (`VramExceeded`), replacing the old static file-size `_fits()`
estimate as the gate. When the GPU is unreadable it falls back to the static VRAM budget so the
existing oversize behavior is preserved.

API:
  acquire(tenant, est_gb=0.0, vram_budget_gb=None)  -> claim the slot (raises LeaseHeld / VramExceeded)
  release(tenant)                                   -> drop our own claim (idempotent)
  reclaim()                                         -> drop a dead holder's stale claim (returns bool)
  current_holder()                                  -> {tenant, pid, acquired_at} of the live holder, or None
  free_vram_gb()                                    -> live free VRAM in GB, or None if unreadable
"""
import contextlib
import fcntl
import json
import os
import subprocess
import time

# Shared lockfile on the WSL filesystem — all three native daemons see the same path. The sidecar
# is a persistent flock coordinator (never unlinked) that serializes the read-decide-claim window.
LEASE_PATH = os.getenv("GPU_LEASE_PATH", "/tmp/mlops-lite-gpu.lease")
_COORD_PATH = LEASE_PATH + ".lock"

# VRAM admission safety margin when falling back to the static budget (matches supervisor._fits()).
_BUDGET_SAFETY = 0.95


class LeaseError(Exception):
    """Base class for lease failures."""


class LeaseHeld(LeaseError):
    """Another live tenant holds the GPU lease (maps to the existing 'GPU busy' rejection)."""

    def __init__(self, holder: dict):
        self.holder = holder or {}
        tenant = self.holder.get("tenant", "another tenant")
        super().__init__(f"GPU busy: {tenant} holds the lease (one GPU tenant, Principle II)")


class VramExceeded(LeaseError):
    """The requested load would exceed live free VRAM (maps to the existing 507 oversize path)."""


def free_vram_gb():
    """Live free VRAM in GB from nvidia-smi, or None if the GPU/tool is unreadable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        return int(out.stdout.strip().splitlines()[0]) / 1024.0
    except Exception:
        return None


def _alive(pid) -> bool:
    """True if `pid` is a live process (os.kill(pid, 0) — FR-063 stale-holder detection).

    Liveness (not a wall-clock TTL) is deliberate (Claude review F4): a training run legitimately
    holds the lease for many minutes/hours, so a TTL-based reclaim could wrongly evict a live long
    run and let a second tenant load onto the GPU — a worse failure than the theoretical case this
    trades against (a dead holder's PID being recycled by an unrelated process before the next
    acquire). On Linux PIDs cycle through a large pid_max space, making that window tiny on a
    single-operator host; liveness keeps long holds safe, which matters more here.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user — still a live holder
    except OSError:
        return False
    return True


def _read_holder():
    """Parse the lockfile into {tenant, pid, acquired_at}, or None if absent/corrupt."""
    try:
        with open(LEASE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    except OSError:
        return None


@contextlib.contextmanager
def _coord():
    """Serialize acquire/release/reclaim across the native daemons (flock; auto-released on death)."""
    fd = os.open(_COORD_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_claim(tenant: str) -> None:
    """Atomically stamp the lockfile with our claim (O_CREAT|O_EXCL — the slot is free here)."""
    fd = os.open(LEASE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"tenant": tenant, "pid": os.getpid(), "acquired_at": time.time()}, f)


def _unlink_quiet() -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(LEASE_PATH)


def acquire(tenant: str, est_gb: float = 0.0, vram_budget_gb: float | None = None) -> dict:
    """Claim the single GPU slot for `tenant`. Atomic — no two callers both proceed (FR-062).

    Raises ``LeaseHeld`` if a live tenant holds it, or ``VramExceeded`` if the estimated footprint
    would exceed live free VRAM (FR-064). Returns the holder record on success. Re-acquiring while
    this process already holds it (same pid) is idempotent.
    """
    with _coord():
        holder = _read_holder()
        if holder is not None:
            hpid = holder.get("pid")
            if hpid == os.getpid():
                _unlink_quiet()  # our own prior/stale claim — replace it (idempotent re-acquire)
            elif _alive(hpid):
                raise LeaseHeld(holder)
            else:
                _unlink_quiet()  # dead holder — self-heal (FR-063)

        # Admission against the live GPU (we hold the slot now; nothing else is loading).
        if est_gb:
            free = free_vram_gb()
            if free is not None:
                if est_gb > free:
                    raise VramExceeded(
                        f"model needs ~{est_gb:.1f} GB but only {free:.1f} GB VRAM is free "
                        f"(live nvidia-smi reading, constitution Principle II / FR-064)")
            elif vram_budget_gb is not None and est_gb > vram_budget_gb * _BUDGET_SAFETY:
                # GPU unreadable — fall back to the static budget (preserves the prior 507 path).
                raise VramExceeded(
                    f"model needs ~{est_gb:.1f} GB but the VRAM budget is {vram_budget_gb:.0f} GB "
                    f"(constitution Principle II / FR-004)")

        _write_claim(tenant)
        return {"tenant": tenant, "pid": os.getpid()}


def release(tenant: str) -> None:
    """Drop our own claim for `tenant` (idempotent; never removes another holder's claim)."""
    with _coord():
        holder = _read_holder()
        if holder and holder.get("pid") == os.getpid() and holder.get("tenant") == tenant:
            _unlink_quiet()


def reclaim() -> bool:
    """Drop a stale claim left by a dead holder (FR-063). Returns True if one was reclaimed."""
    with _coord():
        holder = _read_holder()
        if holder and not _alive(holder.get("pid")):
            _unlink_quiet()
            return True
    return False


def current_holder():
    """The live lease holder {tenant, pid, acquired_at}, or None (used for the UI status, FR-068).

    Intentionally lock-free / stale-tolerant (Claude review F3): it does **not** take `_coord()`, so
    the holder it returns can change the instant after it reads — this is a best-effort *display*
    read (the Infer status line, supervisor `/health`), **not** an admission decision. Admission is
    always made inside `acquire()` under the flock; nothing here arbitrates the GPU.
    """
    holder = _read_holder()
    if holder and _alive(holder.get("pid")):
        return holder
    return None
