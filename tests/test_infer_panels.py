"""009 US1 task-driven Infer panel test (T160 → SC-050).

The Infer tab renders one panel per registry `task` (client-side, from /serving/tasks) via a renderer
map, falling back to a read-only "no renderer" placeholder for an unknown/untagged task (FR-077). The
rendering itself is client-side; this asserts the server-side contract that drives it:
  - the BFF proxies `/api/gw/serving/tasks` (key injected server-side, FR-024),
  - the payload is a list of {model, version, task, serving_engine} — the panel-discovery shape,
  - the Infer tab SSR still has NO model `<select>` (the 008 US3 invariant is preserved), and
  - every entry's task is either a renderable string or null (the placeholder path) — so the tab
    never receives a shape that would break it.

SKIPs cleanly if the UI or key isn't up. Exits non-zero on failure.
"""
import json
import os
import sys
import urllib.error
import urllib.request

UI = f"http://127.0.0.1:{os.getenv('UI_PORT', '3000')}"
KEY = os.getenv("GATEWAY_API_KEY")
# The renderer map's known tasks (ui/components/infer/index.tsx). A task outside this set must be
# tolerated by the UI as a "no renderer" placeholder; the data contract just must not be malformed.
KNOWN_TASKS = {"text-generation", "image-classification", "embedding", "asr", "tabular"}


def _get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _up(url):
    try:
        return _get(url, timeout=3)[0] == 200
    except Exception:
        return False


def main() -> int:
    if not _up(f"{UI}/healthz"):
        print("[SKIP] UI not reachable on :3000 (start it: bash ui/run.sh, or up_all).")
        return 0
    if not KEY:
        print("[SKIP] GATEWAY_API_KEY not set — cannot exercise the authenticated BFF.")
        return 0

    failures = 0

    # 1. BFF proxies /serving/tasks (key injected server-side).
    s, body = _get(f"{UI}/api/gw/serving/tasks")
    ok = s == 200
    print(f"[{'OK' if ok else 'FAIL'}] BFF /api/gw/serving/tasks -> {s} (expect 200)")
    failures += 0 if ok else 1
    tasks = []
    if ok:
        try:
            tasks = json.loads(body).get("tasks", [])
        except Exception as e:
            print(f"[FAIL] /serving/tasks body not JSON: {e}")
            failures += 1

    # 2. Each entry has the panel-discovery shape and a renderable-or-null task (no malformed shape
    #    that would break the tab — FR-077).
    shape_ok = True
    for t in tasks:
        if not all(k in t for k in ("model", "version", "task", "serving_engine")):
            print(f"[FAIL] entry missing discovery fields: {t}")
            shape_ok = False
            continue
        if t["task"] is not None and not isinstance(t["task"], str):
            print(f"[FAIL] entry task is neither str nor null: {t}")
            shape_ok = False
        # Note: an unknown string task is allowed — the UI degrades it to a placeholder.
    print(f"[{'OK' if shape_ok else 'FAIL'}] {len(tasks)} task entr(ies) have a valid discovery shape")
    failures += 0 if shape_ok else 1
    known = [t["task"] for t in tasks if t.get("task") in KNOWN_TASKS]
    print(f"[OK] tasks discovered: {[t.get('task') for t in tasks]} (renderable: {known})")

    # 3. The Infer tab SSR keeps the 008 US3 invariant: no model <select> dropdown.
    s, html_b = _get(f"{UI}/infer")
    html = html_b.decode("utf-8", "ignore") if isinstance(html_b, (bytes, bytearray)) else str(html_b)
    no_dropdown = "<select" not in html
    print(f"[{'OK' if no_dropdown else 'FAIL'}] Infer tab has NO model dropdown (<select removed, FR-069)")
    failures += 0 if no_dropdown else 1

    if failures:
        print(f"\n{failures} panel check(s) failed.")
        return 1
    print("\nT160 PASS — task-driven panel discovery contract holds (per-task render + fallback)")
    return 0


def test_infer_panels(require_ui, require_key):
    """Pytest wrapper (005 US5): needs the console + a key; else skip."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
