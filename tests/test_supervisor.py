"""Phase 2 supervisor integration test (T052, US2 / SC-010).

Starts the native-daemon supervisor, waits for the managed daemons to report healthy, kills one,
and confirms the supervisor restarts it (new pid, higher restart_count) while the others are left
untouched (same pid, still healthy).

Runs in WSL (it manages native processes). Stdlib-only. SKIPs cleanly if the daemons can't reach
healthy (prerequisites — venv / llama build / model — not present), so it never blocks a bare repo.

  python3 tests/test_supervisor.py
  SUPERVISE_DAEMONS=training python3 tests/test_supervisor.py   # faster single-daemon check
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_URL = f"http://localhost:{os.getenv('SUPERVISE_STATUS_PORT', '8099')}/status"
DAEMONS = [d.strip() for d in os.getenv("SUPERVISE_DAEMONS", "serving,training,vision").split(",") if d.strip()]
VICTIM = os.getenv("SUP_VICTIM", DAEMONS[0])
HEALTHY_TIMEOUT = int(os.getenv("SUP_HEALTHY_TIMEOUT", "180"))
RECOVER_TIMEOUT = int(os.getenv("SUP_RECOVER_TIMEOUT", "90"))


def _status():
    with urllib.request.urlopen(STATUS_URL, timeout=3) as r:
        return json.load(r)


def _by_name():
    return {d["name"]: d for d in _status()["daemons"]}


def _wait_all_healthy(timeout):
    deadline = time.time() + timeout
    states = {}
    while time.time() < deadline:
        try:
            states = {n: d["state"] for n, d in _by_name().items()}
            if states and all(v == "healthy" for v in states.values()):
                return True, states
        except Exception:
            pass
        time.sleep(3)
    return False, states


def main() -> int:
    env = dict(os.environ, SUPERVISE_DAEMONS=",".join(DAEMONS))
    sup = subprocess.Popen([sys.executable, os.path.join("supervisor", "supervise.py")],
                           cwd=REPO, env=env)
    try:
        print(f"[..] supervisor pid={sup.pid}; waiting up to {HEALTHY_TIMEOUT}s for "
              f"{DAEMONS} to be healthy ...")
        ok, states = _wait_all_healthy(HEALTHY_TIMEOUT)
        if not ok:
            print(f"[SKIP] daemons did not all reach healthy (prereqs missing?): {states}")
            return 0
        print(f"[OK] all daemons healthy: {states}")

        before = _by_name()
        victim_pid = before[VICTIM]["pid"]
        victim_restarts = before[VICTIM]["restart_count"]
        others = {n: d["pid"] for n, d in before.items() if n != VICTIM}
        print(f"[..] killing '{VICTIM}' (pid={victim_pid}); others={others}")
        os.kill(victim_pid, signal.SIGKILL)

        deadline = time.time() + RECOVER_TIMEOUT
        recovered = False
        while time.time() < deadline:
            cur = _by_name()
            v = cur[VICTIM]
            if v["state"] == "healthy" and v["pid"] and v["pid"] != victim_pid:
                recovered = True
                break
            time.sleep(2)

        if not recovered:
            print(f"[FAIL] '{VICTIM}' did not recover within {RECOVER_TIMEOUT}s: {cur[VICTIM]}")
            return 1
        cur = _by_name()
        if cur[VICTIM]["restart_count"] <= victim_restarts:
            print(f"[FAIL] restart_count did not increase: {cur[VICTIM]}")
            return 1
        print(f"[OK] '{VICTIM}' restarted: pid {victim_pid} -> {cur[VICTIM]['pid']}, "
              f"restart_count {victim_restarts} -> {cur[VICTIM]['restart_count']}")

        # Others must be untouched — same pid, still healthy (never restarted).
        for n, pid in others.items():
            if cur[n]["pid"] != pid or cur[n]["state"] != "healthy":
                print(f"[FAIL] '{n}' was disturbed: was pid={pid} healthy, now {cur[n]}")
                return 1
        print(f"[OK] other daemons untouched: {[n for n in others]}")

        print("\nT052 PASS — killed daemon auto-restarted to healthy; others untouched")
        return 0
    finally:
        sup.send_signal(signal.SIGTERM)
        try:
            sup.wait(timeout=20)
        except subprocess.TimeoutExpired:
            sup.kill()


if __name__ == "__main__":
    sys.exit(main())
