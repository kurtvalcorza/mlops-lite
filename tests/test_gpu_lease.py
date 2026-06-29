"""Race-free GPU lease tests (008 US1, T138 — SC-042/SC-043, FR-062/063/064).

Exercises serving/gpu_lease.py directly (no GPU, no live stack needed — pure lockfile mechanics):

  1. **TOCTOU / one-tenant invariant (SC-042):** N separate processes race to acquire the *same*
     single-slot lease at a barrier; **exactly one** wins each round, the rest get LeaseHeld — never
     two holders. This is the race the two old HTTP guards had and 008 closes atomically.
  2. **Self-healing (FR-063):** a lockfile stamped with a dead PID is reclaimed on the next acquire
     (no permanent deadlock if a holder crashes).
  3. **Live-VRAM admission (SC-043/FR-064):** an oversize estimate is refused via VramExceeded —
     against a live reading when present, falling back to the static budget when the GPU is unreadable.

Runs in WSL (fcntl + multiprocessing fork). Stdlib-only; self-skips off Linux.

  python3 tests/test_gpu_lease.py
"""
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import time

# Point the lease at a private temp path BEFORE importing the module (it reads the path at import).
_LEASE = os.path.join(tempfile.gettempdir(), f"mlops-lease-test-{os.getpid()}.lease")
os.environ["GPU_LEASE_PATH"] = _LEASE

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "serving"))
import gpu_lease  # noqa: E402


def _contend(idx: int, barrier, successes, hold_s: float) -> None:
    """One contender process: race to acquire at the barrier; hold briefly if it wins."""
    barrier.wait()  # all contenders fire acquire at the same instant
    tenant = f"c{idx}"
    try:
        gpu_lease.acquire(tenant)  # est_gb=0 → pure mutex, no VRAM gate
    except gpu_lease.LeaseHeld:
        return  # lost the race — correct
    with successes.get_lock():
        successes.value += 1
    time.sleep(hold_s)  # hold the slot so every loser observes us as the live holder
    gpu_lease.release(tenant)


def _race_once(n: int = 8, hold_s: float = 0.4) -> int:
    barrier = mp.Barrier(n)
    successes = mp.Value("i", 0)
    procs = [mp.Process(target=_contend, args=(i, barrier, successes, hold_s)) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    return successes.value


def _cleanup() -> None:
    for path in (_LEASE, _LEASE + ".lock"):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def main() -> int:
    if os.name != "posix":
        print("[SKIP] gpu_lease needs fcntl + fork (Linux/WSL).")
        return 0
    failures = 0
    _cleanup()
    try:
        # 1. TOCTOU / one-tenant invariant — exactly one winner across repeated rounds.
        rounds, max_seen = 20, 0
        for r in range(rounds):
            won = _race_once()
            max_seen = max(max_seen, won)
            if won != 1:
                print(f"[FAIL] round {r}: {won} processes acquired the single slot (expected 1)")
                failures += 1
                break
        if failures == 0:
            print(f"[OK] one-tenant invariant: exactly 1 holder across {rounds} race rounds "
                  f"(8-way contention; never {max_seen + 1 if max_seen >= 1 else 2} co-resident)")

        # 2. Self-healing: a dead-PID lockfile is reclaimed on the next acquire (FR-063).
        _cleanup()
        with open(_LEASE, "w", encoding="utf-8") as f:
            f.write('{"tenant": "ghost", "pid": 2147480000, "acquired_at": 0}')  # implausible dead PID
        try:
            gpu_lease.acquire("reaper")
            print("[OK] stale (dead-holder) lease reclaimed on acquire — no deadlock (FR-063)")
        except gpu_lease.LeaseHeld:
            print("[FAIL] stale lease not reclaimed — would deadlock the GPU")
            failures += 1
        finally:
            gpu_lease.release("reaper")

        # 3a. Live-VRAM oversize refusal (FR-064): force a tiny live reading.
        _cleanup()
        real_free = gpu_lease.free_vram_gb
        gpu_lease.free_vram_gb = lambda: 2.0  # pretend only 2 GB free
        try:
            gpu_lease.acquire("hog", est_gb=8.0)
            print("[FAIL] oversize load admitted against live free VRAM (FR-064)")
            failures += 1
            gpu_lease.release("hog")
        except gpu_lease.VramExceeded:
            print("[OK] oversize load refused against live free VRAM (VramExceeded, FR-064)")
        finally:
            gpu_lease.free_vram_gb = real_free

        # 3b. Fallback to the static budget when the GPU is unreadable (preserves the 507 path).
        _cleanup()
        gpu_lease.free_vram_gb = lambda: None  # GPU unreadable
        try:
            gpu_lease.acquire("hog", est_gb=20.0, vram_budget_gb=12.0)
            print("[FAIL] oversize load admitted under the static-budget fallback")
            failures += 1
            gpu_lease.release("hog")
        except gpu_lease.VramExceeded:
            print("[OK] oversize load refused via static-budget fallback (GPU unreadable)")
        finally:
            gpu_lease.free_vram_gb = real_free

        # And a fitting load is admitted (sanity: the gate is not stuck closed).
        _cleanup()
        gpu_lease.free_vram_gb = lambda: 11.0
        try:
            gpu_lease.acquire("fits", est_gb=6.6)
            print("[OK] a fitting load is admitted (gate not stuck closed)")
            gpu_lease.release("fits")
        except gpu_lease.VramExceeded:
            print("[FAIL] a fitting load was wrongly refused")
            failures += 1
        finally:
            gpu_lease.free_vram_gb = real_free

        # 4. Corrupt lockfile (crash mid-write) is reclaimed, not a permanent FileExistsError (#2).
        _cleanup()
        with open(_LEASE, "w", encoding="utf-8") as f:
            f.write("{not valid json")  # truncated/corrupt claim
        try:
            gpu_lease.acquire("after-corrupt")
            print("[OK] corrupt lockfile reclaimed on acquire — self-healing intact (#2)")
            gpu_lease.release("after-corrupt")
        except Exception as ex:
            print(f"[FAIL] corrupt lockfile not reclaimed: {type(ex).__name__}: {ex}")
            failures += 1

        # 5. Same-process re-acquire is idempotent and never drops the lease, even if admission
        #    would now fail because our own model is resident and free VRAM is low (#3).
        _cleanup()
        gpu_lease.acquire("llm-serving", est_gb=6.0, vram_budget_gb=12.0)
        gpu_lease.free_vram_gb = lambda: 0.5  # pretend almost no free VRAM now
        try:
            gpu_lease.acquire("llm-serving", est_gb=6.0, vram_budget_gb=12.0)  # same pid+tenant
            held = gpu_lease.current_holder()
            if held and held.get("tenant") == "llm-serving":
                print("[OK] same-process re-acquire idempotent; lease preserved under low VRAM (#3)")
            else:
                print("[FAIL] re-acquire dropped the lease under low VRAM")
                failures += 1
        except gpu_lease.VramExceeded:
            print("[FAIL] re-acquire wrongly re-ran admission and raised VramExceeded (#3)")
            failures += 1
        finally:
            gpu_lease.free_vram_gb = real_free
            gpu_lease.release("llm-serving")

        # 6. PID reuse: a lease stamped with our PID but a mismatched start-time is treated as
        #    stale (the original holder is gone), so it self-heals instead of blocking forever (#7).
        _cleanup()
        with open(_LEASE, "w", encoding="utf-8") as f:
            json.dump({"tenant": "ghost", "pid": os.getpid(), "pid_start": -1, "acquired_at": 0}, f)
        if gpu_lease.current_holder() is None:
            print("[OK] stale lease with mismatched start-time treated as dead — PID-reuse safe (#7)")
        else:
            print("[FAIL] mismatched start-time lease seen as live")
            failures += 1

        # 7. VRAM-owner liveness: a dead owner with a still-alive VRAM child keeps the lease held
        #    (no co-residency after a supervisor crash); reclaimable once the child also exits (#1).
        _cleanup()
        child = subprocess.Popen(["sleep", "30"])
        try:
            with open(_LEASE, "w", encoding="utf-8") as f:
                json.dump({"tenant": "llm-serving", "pid": 2147480000, "pid_start": 1,
                           "vram_pid": child.pid, "vram_start": gpu_lease._proc_start(child.pid),
                           "acquired_at": 0}, f)
            held = gpu_lease.current_holder() is not None
            print(f"[{'OK' if held else 'FAIL'}] orphaned VRAM child keeps the lease held after "
                  f"owner death (#1)")
            failures += 0 if held else 1
            try:
                gpu_lease.acquire("vision")
                print("[FAIL] a second tenant acquired while the orphan child holds VRAM (#1)")
                failures += 1
                gpu_lease.release("vision")
            except gpu_lease.LeaseHeld:
                print("[OK] second tenant refused while the orphan child holds VRAM (#1)")
        finally:
            child.terminate()
            child.wait()
        reclaimable = gpu_lease.reclaim() or gpu_lease.current_holder() is None
        print(f"[{'OK' if reclaimable else 'FAIL'}] lease reclaimable once owner + VRAM child both "
              f"gone (#1)")
        failures += 0 if reclaimable else 1

        # 8. Live owner but DEAD VRAM child → reclaimable: the owner (supervisor) holds no VRAM
        #    itself, so the lease must not stay pinned after its llama-server child dies (#1 fix).
        _cleanup()
        with open(_LEASE, "w", encoding="utf-8") as f:
            json.dump({"tenant": "llm-serving", "pid": os.getpid(),
                       "pid_start": gpu_lease._proc_start(os.getpid()),
                       "vram_pid": 2147480000, "vram_start": 1, "acquired_at": 0}, f)
        if gpu_lease.current_holder() is None:
            print("[OK] live owner + dead VRAM child → lease reclaimable (owner holds no VRAM) (#1)")
        else:
            print("[FAIL] lease pinned by a live owner after its VRAM child died (#1)")
            failures += 1

        # 9. Same-OWNER re-acquire with a dead VRAM child strips the stale vram_pid, so the lease
        #    reverts to owner-PID liveness during the cold reload — otherwise another tenant would see
        #    it as dead (vram-priority) and acquire mid-reload → co-residency (Codex PR#5 P1).
        _cleanup()
        with open(_LEASE, "w", encoding="utf-8") as f:
            json.dump({"tenant": "llm-serving", "pid": os.getpid(),
                       "pid_start": gpu_lease._proc_start(os.getpid()),
                       "vram_pid": 2147480000, "vram_start": 1, "acquired_at": 0}, f)
        pre_reclaimable = gpu_lease.current_holder() is None  # dead vram → looks free to others
        gpu_lease.acquire("llm-serving")  # same owner re-acquires → strips the dead vram_pid
        rec = gpu_lease._read_holder()
        stripped = rec is not None and "vram_pid" not in rec
        held_after = gpu_lease.current_holder() is not None  # now owner-live → held during reload
        ok = pre_reclaimable and stripped and held_after
        print(f"[{'OK' if ok else 'FAIL'}] same-owner re-acquire strips dead vram_pid → lease held by "
              f"owner during reload (PR#5 P1)")
        failures += 0 if ok else 1
        gpu_lease.release("llm-serving")
    finally:
        _cleanup()

    if failures:
        print(f"\n{failures} lease check(s) failed.")
        return 1
    print("\nT138 PASS — atomic one-tenant lease (no TOCTOU), self-healing (dead-PID, corrupt-file, "
          "PID-reuse, orphaned-VRAM-child), idempotent re-acquire, live-VRAM admission")
    return 0


def test_gpu_lease(require_wsl):
    """Pytest wrapper: needs fcntl + fork (WSL); pure lockfile mechanics, no GPU/stack required."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
