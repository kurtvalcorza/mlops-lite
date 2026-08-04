"""003 US2 operator-console smoke + live-state test (T071, FR-029 / SC-017).

Run in WSL (it kills a daemon by pid). Asserts:
  - the UI is reachable and every one of the six surfaces returns 200 through the Next server,
  - the BFF proxies `/platform/health` (key injected server-side),
  - the SSE `state` channel reflects supervision: kill a daemon by its supervisor-reported pid and
    watch the channel flip that daemon unreachable, then healthy again once the supervisor restarts
    it (ties the Health tab to US2 supervision). Net system state is unchanged afterwards.

SKIPs cleanly if the UI, gateway key, or supervisor isn't up. Exits non-zero on failure.
The kill-flip targets `training` (fast restart, no VRAM impact when idle); override via FLIP_DAEMON.
"""
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

UI = f"http://127.0.0.1:{os.getenv('UI_PORT', '3000')}"
GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"
SUPERVISOR = f"http://localhost:{os.getenv('SUPERVISE_STATUS_PORT', '8099')}"
KEY = os.getenv("GATEWAY_API_KEY")
FLIP = os.getenv("FLIP_DAEMON", "training")
# 027 T706: the ten areas. The 021 paths below them are kept in `RETIRED` rather than dropped —
# a smoke test that stopped exercising them would stop noticing the day a redirect breaks, which is
# the day every bookmark and runbook link in the platform breaks with it.
AREAS = ["/overview", "/models", "/training", "/evaluations", "/deployments", "/inference",
         "/datasets", "/runtime", "/observability", "/administration"]
RETIRED = ["/infer", "/runs", "/monitor", "/monitoring", "/serving", "/data", "/retraining",
           "/health"]
PAGES = AREAS + RETIRED


def _get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _up(url):
    try:
        s, _ = _get(url, timeout=3)
        return s == 200
    except Exception:
        return False


def _supervisor_pid(name):
    s, body = _get(f"{SUPERVISOR}/status", timeout=5)
    if s != 200:
        return None
    for d in json.loads(body).get("daemons", []):
        if d["name"] == name:
            return d.get("pid")
    return None


def _daemon_reachable(name):
    """Read one state frame from the gateway SSE channel; return that daemon's reachability."""
    req = urllib.request.Request(f"{GW}/platform/events", headers={"X-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("event") == "state":
                return ev.get("daemons", {}).get(name, {}).get("reachable")
    return None


def _wait_reachable(name, want, bound_s):
    deadline = time.time() + bound_s
    while time.time() < deadline:
        if _daemon_reachable(name) == want:
            return True
        time.sleep(2)
    return False


def main() -> int:
    if not _up(f"{UI}/healthz"):
        print("[SKIP] UI not reachable on :3000 (start it: bash ui/run.sh, or up_all).")
        return 0
    if not KEY:
        print("[SKIP] GATEWAY_API_KEY not set — cannot exercise the authenticated BFF/SSE.")
        return 0

    failures = 0

    # 1. UI healthz + all six surfaces reachable through the Next server.
    print("[OK] UI /healthz reachable")
    for p in PAGES:
        s, _ = _get(UI + p)
        ok = s == 200
        print(f"[{'OK' if ok else 'FAIL'}] UI {p} -> {s} (expect 200)")
        failures += 0 if ok else 1

    # 1b. 027 T706: Overview is the LANDING view. 021 landed on /serving — right when the IA *was*
    #     the loop, wrong now that there are ten areas of concern and no ordering among them.
    s, body = _get(UI + "/")
    html = body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else str(body)
    landed = s == 200 and "Overview" in html
    print(f"[{'OK' if landed else 'FAIL'}] UI / lands on Overview -> {s} (027 FR-364)")
    failures += 0 if landed else 1

    # 1c. 027 T706: the summary cards and the active-work join populate through the BFF. Asserted
    #     against the ROUTES rather than the rendered HTML — the panels render client-side, and a
    #     test that scraped the SSR shell would pass while every card said "unknown".
    for route in ("console/summary", "console/attention", "console/activity", "console/jobs"):
        s, body = _get(f"{UI}/api/gw/{route}")
        ok = s == 200
        detail = ""
        if ok:
            try:
                envelope = json.loads(body)
                # The envelope is the contract. A 200 carrying the wrong shape would let every
                # truthfulness property downstream fail silently.
                ok = set(envelope) >= {"data", "observed", "degraded"}
                detail = f" degraded={envelope.get('degraded')}"
            except Exception:
                ok = False
        print(f"[{'OK' if ok else 'FAIL'}] BFF /api/gw/{route} -> {s} (envelope){detail}")
        failures += 0 if ok else 1

    # 2. BFF proxies /platform/health (key injected server-side).
    s, _ = _get(f"{UI}/api/gw/platform/health")
    ok = s == 200
    print(f"[{'OK' if ok else 'FAIL'}] BFF /api/gw/platform/health -> {s} (expect 200)")
    failures += 0 if ok else 1

    # 2b. 008 US3 (FR-069/SC-046) + 009 US1 (FR-077): the Infer tab dropped the inert model `<select>`,
    #     and 009 made the panels task-driven — they render client-side from /serving/tasks, so the
    #     "serving:" status line is no longer in the SSR HTML (it appears after task discovery). The
    #     no-`<select>` invariant still holds in SSR, and the tab must render its discovery shell.
    s, body = _get(f"{UI}/infer")
    html = body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else str(body)
    # Tight positive assertion (Claude review): the SSR shell emits the task-discovery placeholder
    # from page.tsx ("discovering tasks…") before hydration — a broad "infer" substring would also
    # pass on an error page that merely mentions infer.
    renders = s == 200 and "discovering tasks" in html.lower()
    no_dropdown = "<select" not in html
    print(f"[{'OK' if renders else 'FAIL'}] Infer tab renders (200, task-driven discovery shell — 009 FR-077)")
    print(f"[{'OK' if no_dropdown else 'FAIL'}] Infer tab has NO model dropdown (<select removed, FR-069)")
    failures += (0 if renders else 1) + (0 if no_dropdown else 1)

    # 2c. 008 US3 (FR-068): the BFF proxies the new GPU/lease state route (key injected server-side).
    s, _ = _get(f"{UI}/api/gw/serving/state")
    ok = s == 200
    print(f"[{'OK' if ok else 'FAIL'}] BFF /api/gw/serving/state -> {s} (expect 200, lease state)")
    failures += 0 if ok else 1

    # 3. SSE state channel reflects supervision (kill -> unreachable -> restarted -> reachable).
    if not _up(f"{SUPERVISOR}/status"):
        print("[SKIP] supervisor (:8099) not running — cannot exercise the kill-flip; "
              "state-channel read still validated above.")
    elif _daemon_reachable(FLIP) is not True:
        print(f"[SKIP] '{FLIP}' not currently healthy on the channel — skipping kill-flip.")
    else:
        pid = _supervisor_pid(FLIP)
        if not pid:
            print(f"[SKIP] no supervisor pid for '{FLIP}'.")
        else:
            print(f"[..] killing '{FLIP}' (pid {pid}) — supervisor should restart it")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            went_down = _wait_reachable(FLIP, False, bound_s=20)
            print(f"[{'OK' if went_down else 'FAIL'}] channel shows '{FLIP}' unreachable after kill")
            failures += 0 if went_down else 1
            came_back = _wait_reachable(FLIP, True, bound_s=90)
            print(f"[{'OK' if came_back else 'FAIL'}] channel shows '{FLIP}' healthy again "
                  f"(supervisor restarted it)")
            failures += 0 if came_back else 1

    if failures:
        print(f"\n{failures} smoke check(s) failed.")
        return 1
    print("\nT071/T706 PASS — ten areas + every retired path reachable; Overview is the landing "
          "view; the console read envelopes proxy; SSE state channel tracks supervision live")
    return 0


def test_ui_smoke(require_ui, require_key):
    """Pytest wrapper (005 US5): needs the console + a key; else skip (kill-flip self-skips)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
