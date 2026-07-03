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

# Shared lockfile on the WSL filesystem — every GPU daemon must see the SAME path. 018 US1
# (FR-166): the default moved OFF /tmp into the fixed platform state dir. /tmp is wiped per
# reboot and can silently differ across mount namespaces / WSL distros — two daemons with
# different /tmp views would each "hold" their own lease file, voiding the single-GPU mutex with
# no error anywhere. The canonical definition is `platformlib.topology.STATE_DIR` (env
# `MLOPS_STATE_DIR`, default ~/.mlops-lite); the same env+default is computed inline as a
# fallback so this module stays importable standalone (the offline tests and the daemons load it
# with only serving/ on the path). `GPU_LEASE_PATH` remains an explicit override.
try:
    import sys as _sys

    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in _sys.path:
        _sys.path.insert(0, _repo_root)
    from platformlib.topology import STATE_DIR  # noqa: E402
except Exception:  # pragma: no cover — same contract as platformlib.topology.STATE_DIR
    STATE_DIR = os.path.expanduser(os.getenv("MLOPS_STATE_DIR", "~/.mlops-lite"))

LEASE_PATH = os.getenv("GPU_LEASE_PATH", os.path.join(STATE_DIR, "gpu.lease"))
_COORD_PATH = LEASE_PATH + ".lock"
_BEACON_PATH = os.path.join(os.path.dirname(LEASE_PATH) or ".", "state-dir.beacon")

#: The pre-018 default lease location. During a mixed-version upgrade window an OLD daemon may
#: still arbitrate via this file (it knows nothing of MLOPS_STATE_DIR or the beacon), so a fresh
#: claim here also honors a LIVE holder there (internal review, 018) — otherwise the old and new
#: lease domains would each admit a tenant. The old side cannot see OUR lease, so this closes
#: only the new-side half of the window; it retires with the lockfile at T364. Env override is
#: a test seam.
_LEGACY_TMP_LEASE = os.getenv("MLOPS_LEGACY_LEASE_PATH", "/tmp/mlops-lite-gpu.lease")

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


def _proc_start(pid):
    """Process start-time (clock ticks since boot, /proc/<pid>/stat field 22), or None.

    Recorded alongside the PID so a *recycled* PID is distinguishable from the original holder
    (Codex P2 #7): a reused PID belongs to a different process with a different start-time, so the
    stale lease is not mistaken for a live tenant. The comm field (2) can contain spaces/parens, so
    parse the fields *after* the final ')'.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
        return int(data[data.rfind(")") + 1:].split()[19])  # field 22 → index 19 after state
    except (OSError, ValueError, IndexError):
        return None


def _alive(pid, start=None) -> bool:
    """True if `pid` is the live process that recorded the lease (os.kill + start-time match).

    Liveness (not a wall-clock TTL) is deliberate (Claude review F4): a training run legitimately
    holds the lease for many minutes/hours, so a TTL-based reclaim could wrongly evict a live long
    run and let a second tenant load onto the GPU — a worse failure than the case it trades against.
    The PID-reuse gap that liveness alone leaves (Codex #7) is closed by also matching the recorded
    start-time when present: a process that reused the PID has a different start-time → treated as
    dead → the stale lease self-heals.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists but owned by another user — can't signal it, but /proc/<pid>/stat is
        # world-readable so the start-time check below still runs (PID-reuse stays detectable)
    except OSError:
        return False
    if start is not None:
        cur = _proc_start(pid)
        if cur is not None and cur != start:
            return False  # same PID, different process (recycled) — the original holder is gone
    return True


def _holder_live(holder) -> bool:
    """A lease is live while its **VRAM is occupied** (Codex P1 #1).

    When a `vram_pid` is recorded (the LLM's `llama-server` child), that child's liveness *is* the
    lease's — the owner daemon (supervisor) holds no VRAM itself, so the lease must NOT stay held
    just because the supervisor is alive after its child died (that would pin the GPU while VRAM is
    actually free). Conversely an orphaned child after a supervisor crash keeps the lease held (its
    VRAM is still allocated), so no second tenant co-resides. With no `vram_pid` (vision/training
    hold VRAM in-process, and the LLM before its child is registered), liveness is the owner PID.
    The supervisor re-claims its own lease across a child crash via acquire()'s same-(owner-)PID
    idempotent path, independent of this check.
    """
    if not holder:
        return False
    vram_pid = holder.get("vram_pid")
    if vram_pid is not None:
        return _alive(vram_pid, holder.get("vram_start"))
    return _alive(holder.get("pid"), holder.get("pid_start"))


def _read_holder():
    """Parse the lockfile into the holder record, or None if absent/corrupt/unreadable."""
    try:
        with open(LEASE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    except OSError:
        return None


def _lease_exists() -> bool:
    return os.path.exists(LEASE_PATH)


def _ours(holder) -> bool:
    """True when the recorded owner is THIS live process — PID plus start-time when recorded.

    The same-pid branch previously trusted the PID alone (internal review, 018): a stale record
    whose PID got recycled to US would be treated as "already ours" and returned as a live claim,
    while peers — seeing the start-time mismatch — would reclaim the file. Two tenants. A record
    with no `pid_start` (pre-018 format) keeps the old PID-only behavior.
    """
    if holder.get("pid") != os.getpid():
        return False
    start = holder.get("pid_start")
    return start is None or start == _proc_start(os.getpid())


def _legacy_tmp_holder():
    """A LIVE holder found at the pre-018 /tmp lease path, or None (see _LEGACY_TMP_LEASE)."""
    try:
        if os.path.realpath(_LEGACY_TMP_LEASE) == os.path.realpath(LEASE_PATH):
            return None  # explicitly pointed at the legacy path — one domain, nothing to shadow
        with open(_LEGACY_TMP_LEASE, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if _holder_live(rec) else None


#: The FIXED rendezvous pointer — deliberately independent of the (configurable)
#: MLOPS_STATE_DIR/GPU_LEASE_PATH, so two daemons pointed at DIFFERENT state dirs on the same
#: host still collide here (Codex review, 018: a private dir yields a self-consistent beacon,
#: so the beacon alone can't catch a same-host override/typo). Captured at import like
#: LEASE_PATH; env override exists for tests.
_POINTER_PATH = os.getenv("MLOPS_STATE_POINTER",
                          os.path.join(os.path.expanduser("~/.mlops-lite"),
                                       "state-pointer.json"))


def _pointer_path() -> str:
    return _POINTER_PATH


def _verify_pointer(component: str, coord_dir: str) -> None:
    """First participant records the canonical coordination dir at the fixed pointer; every later
    participant must resolve to the SAME dir or fail loud (with the fix in the message)."""
    pointer = _pointer_path()
    os.makedirs(os.path.dirname(pointer) or ".", exist_ok=True)
    real = os.path.realpath(coord_dir)
    # Write-temp + hard-link claim (Codex round 4, 018): O_EXCL-create + json.dump left a window
    # where a concurrently-starting peer read a partial/empty pointer and aborted a NORMAL
    # parallel launch. The link is atomic and only publishes a COMPLETE file; the loser of the
    # link falls through to the read-and-compare path below.
    tmp = f"{pointer}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"state_dir": real, "created_by": component,
                       "created_at": time.time()}, f)
        try:
            os.link(tmp, pointer)
            return
        except FileExistsError:
            pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    try:
        with open(pointer, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        raise LeaseError(f"{component}: unreadable state pointer {pointer}: {e}") from e
    recorded = rec.get("state_dir")
    if recorded and os.path.realpath(recorded) != real:
        raise LeaseError(
            f"{component}: GPU coordination dir MISMATCH — this daemon uses {real} but "
            f"{rec.get('created_by', 'a peer')} registered {recorded} at {pointer}. Two private "
            f"lease files would allow two GPU tenants (Principle II). Point every daemon at ONE "
            f"MLOPS_STATE_DIR; if the move is intentional, stop all GPU daemons and delete "
            f"{pointer} first.")


def verify_shared_state_dir(component: str) -> dict:
    """Startup invariant check (018 US1, FR-166): every GPU participant must observe the SAME
    coordination state as its peers — fail LOUD on divergence, never run with a private mutex.

    Two layers:
      1. **Fixed rendezvous pointer** (`_verify_pointer`): catches two daemons on the same host
         configured with DIFFERENT state dirs — the case a per-dir beacon cannot see, because a
         private dir's beacon is self-consistent (Codex review, 018).
      2. **Per-dir beacon**: records, at first creation, the identity of the filesystem view that
         created it — hostname + WSL distro + the beacon's own (st_dev, st_ino). A participant
         that reads a beacon recorded by a DIFFERENT host/distro, or whose stat of the file
         disagrees with the recorded inode (a copied/recreated file — two views that are not the
         same file), is seeing a divergent state dir. Either failure raises `LeaseError` so the
         daemon exits at startup instead of silently co-residing on the GPU later.
    """
    import socket

    _verify_pointer(component, os.path.dirname(_BEACON_PATH) or ".")
    ident = {"node": socket.gethostname(), "distro": os.getenv("WSL_DISTRO_NAME", "")}
    os.makedirs(os.path.dirname(_BEACON_PATH) or ".", exist_ok=True)
    written = _write_beacon(component, ident, claim=True)  # atomic first-creation claim
    if written is not None:
        return written
    try:
        with open(_BEACON_PATH, encoding="utf-8") as f:
            rec = json.load(f)
        st = os.stat(_BEACON_PATH)
    except (OSError, ValueError) as e:
        raise LeaseError(f"{component}: unreadable state-dir beacon {_BEACON_PATH}: {e}") from e
    problems = []
    if rec.get("node") != ident["node"] or rec.get("distro") != ident["distro"]:
        problems.append(f"beacon created on {rec.get('node')!r}/{rec.get('distro')!r}, "
                        f"this daemon runs on {ident['node']!r}/{ident['distro']!r}")
    if (st.st_dev, st.st_ino) != (rec.get("dev"), rec.get("ino")):
        problems.append("beacon file identity changed (copied/recreated — not the same file "
                        "the recording participant saw)")
    if problems:
        # Self-heal when the GPU is IDLE (internal review, 018): a reboot/hostname change or a
        # restored state dir tripped this check on every daemon at once — a permanent brick until
        # an operator deleted the beacon by hand. With NO live lease holder there is no
        # co-residency to protect, so re-stamping under the current identity is safe. With a live
        # holder we still refuse: divergence while the GPU is occupied is exactly the split-brain
        # this beacon exists to catch.
        if not _holder_live(_read_holder()):
            return _write_beacon(component, ident, claim=False, healed_from=problems)
        raise LeaseError(
            f"{component}: GPU coordination state dir DIVERGES from peers "
            f"({'; '.join(problems)}). All GPU daemons must share one filesystem view of "
            f"{os.path.dirname(_BEACON_PATH)} (MLOPS_STATE_DIR) — refusing to start rather than "
            f"risk two GPU tenants with private leases (Principle II).")
    return rec


def _write_beacon(component: str, ident: dict, *, claim: bool, healed_from=None):
    """Publish the beacon atomically (write-temp + link/replace — a peer never reads a partial
    file, Codex round 4). `claim=True` only creates (None if one already exists); `claim=False`
    overwrites (the idle self-heal path). link/rename preserve the inode, so the recorded
    (dev, ino) identity stays true for the published file."""
    tmp = f"{_BEACON_PATH}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            st = os.fstat(f.fileno())
            rec = {**ident, "dev": st.st_dev, "ino": st.st_ino,
                   "created_by": component, "created_at": time.time()}
            if healed_from:
                rec["healed_from"] = healed_from
            json.dump(rec, f)
        if claim:
            try:
                os.link(tmp, _BEACON_PATH)
            except FileExistsError:
                return None
        else:
            os.replace(tmp, _BEACON_PATH)
        return rec
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@contextlib.contextmanager
def _coord():
    """Serialize acquire/release/reclaim across the native daemons (flock; auto-released on death)."""
    os.makedirs(os.path.dirname(_COORD_PATH) or ".", exist_ok=True)  # the state dir (FR-166)
    fd = os.open(_COORD_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_claim(tenant: str) -> dict:
    """Atomically stamp the lockfile with our claim (O_CREAT|O_EXCL — the slot is free here) and
    return the written record.

    Records the owner PID **and its start-time** so a recycled PID can't be mistaken for us (#7).
    """
    pid = os.getpid()
    rec = {"tenant": tenant, "pid": pid, "pid_start": _proc_start(pid), "acquired_at": time.time()}
    fd = os.open(LEASE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return rec


def _unlink_quiet() -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(LEASE_PATH)


def _replace_holder(rec: dict) -> None:
    """Atomically overwrite the lockfile with `rec` (write-temp + os.replace). Caller holds _coord()."""
    tmp = f"{LEASE_PATH}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, LEASE_PATH)


def acquire(tenant: str, est_gb: float = 0.0, vram_budget_gb: float | None = None) -> dict:
    """Claim the single GPU slot for `tenant`. Atomic — no two callers both proceed (FR-062).

    Raises ``LeaseHeld`` if a live tenant holds it, or ``VramExceeded`` if the estimated footprint
    would exceed live free VRAM (FR-064). Returns the holder record on success. Re-acquiring while
    this process already holds it (same pid) is idempotent.
    """
    with _coord():
        holder = _read_holder()
        if holder is not None:
            if _ours(holder):  # pid AND start-time — a recycled-PID stale record is NOT ours
                if holder.get("tenant") == tenant:
                    # Already ours — idempotent re-acquire. Return the on-disk record WITHOUT deleting
                    # the claim or re-running admission (Codex #3): re-running VRAM admission while our
                    # own model is resident (free VRAM low) could raise and leave us with no lease.
                    vram_pid = holder.get("vram_pid")
                    if vram_pid is not None and not _alive(vram_pid, holder.get("vram_start")):
                        # Our previous VRAM child died and we're cold-restarting. Strip the stale
                        # vram_pid so liveness reverts to our (live) owner PID during the reload —
                        # otherwise _holder_live (which keys on vram_pid when present) would treat the
                        # lease as dead and another tenant could acquire mid-reload → co-residency
                        # (Codex PR#5 P1). set_vram_owner() re-stamps the new child once it's up.
                        holder.pop("vram_pid", None)
                        holder.pop("vram_start", None)
                        _replace_holder(holder)
                    return holder
                # Same process, DIFFERENT tenant. The host agent is ONE process hosting several
                # tenant identities via the interop shim. If our own model is genuinely still
                # resident — a LIVE recorded VRAM child — refuse, else loading tenant B would
                # co-reside with the resident tenant A (co-residency with no error, Principle II).
                vram_pid = holder.get("vram_pid")
                if vram_pid is not None and _alive(vram_pid, holder.get("vram_start")):
                    raise LeaseHeld(holder)
                # No live VRAM child: this is our OWN stale claim — e.g. a release that was dropped
                # or failed after the model already unloaded (admission.release swallows a failed
                # lease.release). Refusing here would wedge every OTHER tenant behind a live-PID
                # record until the agent restarts (019/US2, FR-190). Self-heal our own record and
                # fall through to a fresh claim; a DIFFERENT live PID is still refused by the
                # _holder_live branch below.
                _unlink_quiet()
            elif _holder_live(holder):
                raise LeaseHeld(holder)
            else:
                _unlink_quiet()  # dead holder (owner + any VRAM child both gone) — self-heal (FR-063)
        elif _lease_exists():
            # Lockfile present but unreadable/corrupt (e.g. a crash between O_EXCL create and the
            # json.dump) — reclaim it, else the O_EXCL write below would FileExistsError and every
            # tenant would fail instead of self-healing (Codex P1 #2).
            _unlink_quiet()

        # Mixed-version upgrade guard (internal review, 018): an OLD daemon still on the pre-018
        # /tmp lease path can't see our lease at all — the least we can do is honor a live holder
        # in ITS domain before claiming ours, so the NEW side never co-resides with it.
        legacy = _legacy_tmp_holder()
        if legacy is not None:
            raise LeaseHeld({**legacy, "domain": f"legacy lease {_LEGACY_TMP_LEASE}"})

        # Admission against the live GPU. Only a FRESH claim reaches here (a same-tenant re-acquire
        # returned above), so if this raises, no valid lease is lost — the slot is genuinely empty.
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

        return _write_claim(tenant)  # full record (tenant, pid, pid_start, acquired_at)


def set_vram_owner(tenant: str, vram_pid: int) -> None:
    """Record the process that actually holds VRAM (e.g. the LLM's `llama-server` child) on our
    lease (Codex P1 #1). Acquire stamps the owner *daemon* PID before the child exists; the daemon
    calls this once the child is up. The lease then stays live if the daemon crashes but the child
    orphans (its VRAM is still allocated). Only the owner (same pid + tenant) may set it; no-op
    otherwise. Atomic via os.replace.
    """
    with _coord():
        holder = _read_holder()
        if holder and holder.get("pid") == os.getpid() and holder.get("tenant") == tenant:
            holder["vram_pid"] = vram_pid
            holder["vram_start"] = _proc_start(vram_pid)
            _replace_holder(holder)


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
        if holder and not _holder_live(holder):
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
    if _holder_live(holder):
        return holder
    return None
