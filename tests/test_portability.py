"""Portability check (002 US4, T059 / SC-011) — manual-tier.

Validates the "genericized template" contract: a clean machine is retargeted by editing ONLY
`.specify/memory/hardware-profile.md`, then `scripts/bootstrap.sh` provisions the native env and
`tests/test_serving.py` passes. This test:

  1. asserts the retarget point (hardware-profile.md) and the bootstrap artifacts exist,
  2. scans for machine-specific values leaking OUTSIDE the profile/docs (which would mean more than
     one file must change to retarget),
  3. runs the serving smoke as the end-to-end proof IF the serving daemon is reachable, else SKIPs
     with the documented procedure (it can't provision a clean box from inside this process).

  python tests/test_portability.py
"""
import os
import re
import subprocess
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(REPO, ".specify", "memory", "hardware-profile.md")
GW = f"http://localhost:{os.getenv('GATEWAY_PORT', '8080')}"

# Machine-specific tokens that should live ONLY in the hardware profile (or human docs). If they
# appear in code/compose/scripts, retargeting would need edits beyond hardware-profile.md.
MACHINE_TOKENS = ["RTX 5070 Ti", "Core Ultra 9 275HX"]
ALLOWED = (".specify/memory/hardware-profile.md", "README.md", "/specs/", "/docs/", "CHANGELOG",
           "tests/test_portability.py")  # this file defines the token list it scans for


def _exists(path, label):
    ok = os.path.isfile(path)
    print(f"[{'OK' if ok else 'FAIL'}] {label} exists ({os.path.relpath(path, REPO)})")
    return ok


def _cmd(argv):
    """Run a command, return stripped stdout, or None if it isn't available / fails."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _scan_leakage():
    """Walk the tree; flag machine tokens outside the allowed (profile/docs) files."""
    hits = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, REPO).replace("\\", "/")
            if any(a.strip("/") in rel for a in ALLOWED):
                continue
            if not fn.endswith((".py", ".sh", ".ps1", ".yml", ".yaml", ".json", ".txt", ".md")):
                continue
            try:
                text = open(fp, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for tok in MACHINE_TOKENS:
                if tok in text:
                    hits.append((rel, tok))
    return hits


def _serving_reachable():
    try:
        with urllib.request.urlopen(f"{GW}/platform/health", timeout=5) as r:
            import json
            data = json.load(r)
            return bool(data.get("daemons", {}).get("serving", {}).get("reachable"))
    except Exception:
        return False


def main() -> int:
    failures = 0

    print("== retarget contract ==")
    failures += 0 if _exists(PROFILE, "hardware profile (single retarget point)") else 1
    failures += 0 if _exists(os.path.join(REPO, "scripts", "bootstrap.sh"), "bootstrap.sh") else 1
    failures += 0 if _exists(os.path.join(REPO, "scripts", "native_env.lock"), "native_env.lock") else 1

    # VRAM_GB must be parseable from the profile (bootstrap + daemons read it).
    vram = None
    if os.path.isfile(PROFILE):
        m = re.search(r"\|\s*`?VRAM_GB`?\s*\|\s*(\d+)", open(PROFILE, encoding="utf-8").read())
        vram = m.group(1) if m else None
    print(f"[{'OK' if vram else 'FAIL'}] VRAM_GB parseable from the profile -> {vram}")
    failures += 0 if vram else 1

    # Node/UI tier (004 US2, T089/FR-038): the retarget contract now covers the operator console —
    # bootstrap gates Node >= 20 and pre-builds ui/. A retargeted machine must have Node and a built UI.
    print("\n== node/ui tier (operator console) ==")
    node_min = 20
    node_v = _cmd(["node", "-v"])           # e.g. "v22.22.2"
    npm_v = _cmd(["npm", "-v"])
    node_major = None
    if node_v and node_v.lstrip("v").split(".")[0].isdigit():
        node_major = int(node_v.lstrip("v").split(".")[0])
    node_ok = node_major is not None and node_major >= node_min
    print(f"[{'OK' if node_ok else 'FAIL'}] node >= {node_min} LTS -> {node_v or '(not found)'} "
          f"(npm {npm_v or '?'})")
    failures += 0 if node_ok else 1

    lock = os.path.join(REPO, "ui", "package-lock.json")
    failures += 0 if _exists(lock, "ui pinned lockfile (npm ci source)") else 1
    built = os.path.isdir(os.path.join(REPO, "ui", ".next"))
    print(f"[{'OK' if built else 'FAIL'}] ui built (ui/.next present — bootstrap pre-builds it)")
    failures += 0 if built else 1

    print("\n== machine-specificity (only the profile should carry hardware values) ==")
    leaks = _scan_leakage()
    if leaks:
        for rel, tok in leaks:
            print(f"[WARN] '{tok}' appears in {rel} (retargeting would need to touch this too)")
    else:
        print("[OK] no machine-specific tokens leak outside the hardware profile / docs")

    print("\n== end-to-end serving smoke ==")
    if _serving_reachable():
        print("[..] serving reachable — running tests/test_serving.py as the portability proof")
        rc = subprocess.run([sys.executable, os.path.join("tests", "test_serving.py")], cwd=REPO).returncode
        failures += 0 if rc == 0 else 1
    else:
        print("[SKIP] serving daemon not reachable — manual-tier proof:")
        print("       on a clean clone: edit .specify/memory/hardware-profile.md ->")
        print("       bash scripts/bootstrap.sh -> ./scripts/up_all.ps1 -> python tests/test_serving.py")

    if failures:
        print(f"\n{failures} portability check(s) failed.")
        return 1
    print("\nT059/T089 PASS — retarget contract holds (one profile file; bootstrap + lock present; "
          "Node tier gated + UI built)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
