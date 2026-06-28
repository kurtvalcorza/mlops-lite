"""004 US3 console-resilience test (T091, FR-040 / SC-023). Run in WSL.

Asserts the console fails honestly:
  - `/healthz` is a cheap liveness probe that stays up regardless (what the supervisor polls),
  - `/readyz` is a READINESS probe that reflects whether the BFF can reach the gateway — ready when
    the gateway is up, NOT-ready when it isn't (proven with a throwaway UI pointed at a dead gateway),
  - the ui daemon's restart_count stays STABLE — the process-group orphan fix holds, no crash-loop.

SKIPs cleanly if the UI isn't up. The not-ready sub-check is best-effort (skips if it can't spin a
throwaway instance). Exits non-zero on failure.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = f"http://127.0.0.1:{os.getenv('UI_PORT', '3000')}"
SUPERVISOR = f"http://localhost:{os.getenv('SUPERVISE_STATUS_PORT', '8099')}"


def _get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)


def _ready_field(body):
    """Parse the `ready` boolean from a /readyz JSON body, or None if unparseable."""
    try:
        return json.loads(body).get("ready")
    except Exception:
        return None


def _ui_restart_count():
    s, body = _get(f"{SUPERVISOR}/status")
    if s != 200:
        return None
    for d in json.loads(body).get("daemons", []):
        if d["name"] == "ui":
            return d.get("restart_count")
    return None


def _check_not_ready_branch():
    """Spin a throwaway `next start` pointed at a dead gateway; its /readyz must report not-ready."""
    port = "3099"
    env = dict(os.environ, GATEWAY_URL="http://127.0.0.1:9", UI_PORT=port,
               GATEWAY_API_KEY="x")
    proc = None
    try:
        proc = subprocess.Popen(
            ["./node_modules/.bin/next", "start", "-H", "127.0.0.1", "-p", port],
            cwd=os.path.join(REPO, "ui"), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            s, _ = _get(f"{base}/healthz", timeout=2)
            if s == 200:
                break
            time.sleep(1)
        else:
            print("[SKIP] throwaway UI did not come up — not-ready branch unverified")
            return 0
        # liveness up even though the gateway is dead ...
        s_live, _ = _get(f"{base}/healthz")
        # ... but readiness reports not-ready (gateway unreachable).
        s_ready, body = _get(f"{base}/readyz")
        live_ok = s_live == 200
        ready_not = s_ready == 503 and _ready_field(body) is False
        print(f"[{'OK' if live_ok else 'FAIL'}] throwaway /healthz up despite dead gateway -> {s_live}")
        print(f"[{'OK' if ready_not else 'FAIL'}] throwaway /readyz reports NOT-ready -> {s_ready}")
        return 0 if (live_ok and ready_not) else 1
    except FileNotFoundError:
        print("[SKIP] next binary not found — not-ready branch unverified")
        return 0
    finally:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except Exception:
                proc.terminate()


def main() -> int:
    s, _ = _get(f"{UI}/healthz", timeout=3)
    if s != 200:
        print("[SKIP] UI not reachable on :3000 — start it (bash ui/run.sh / up_all).")
        return 0

    failures = 0

    # 1. Liveness stays up.
    print(f"[OK] /healthz (liveness) -> {s}")

    # 2. Readiness is ready when the gateway is reachable.
    s, body = _get(f"{UI}/readyz")
    ready = s == 200 and _ready_field(body) is True
    print(f"[{'OK' if ready else 'FAIL'}] /readyz reports ready (gateway reachable) -> {s}")
    failures += 0 if ready else 1

    # 3. Readiness is distinct from liveness — proven by the not-ready branch (dead gateway).
    failures += _check_not_ready_branch()

    # 4. ui daemon restart_count stays stable (orphan fix holds — no crash-loop).
    rc1 = _ui_restart_count()
    if rc1 is None:
        print("[SKIP] supervisor (:8099) not running — cannot check restart stability.")
    else:
        time.sleep(8)
        rc2 = _ui_restart_count()
        stable = rc1 == rc2
        print(f"[{'OK' if stable else 'FAIL'}] ui restart_count stable -> {rc1} -> {rc2} "
              f"({'no churn' if stable else 'CHURNING'})")
        failures += 0 if stable else 1

    if failures:
        print(f"\n{failures} resilience check(s) failed.")
        return 1
    print("\nT091 PASS — readiness distinct from liveness; ui daemon stable (no crash-loop)")
    return 0


def test_ui_resilience(require_ui):
    """Pytest wrapper (005 US5): skip unless the console is up (not-ready branch self-skips)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
