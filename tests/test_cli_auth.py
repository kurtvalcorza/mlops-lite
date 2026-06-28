"""005 US3 operator-CLI auth-parity test (T098, FR-043 / SC-027). Run against a keyed stack.

Proves the shipped CLIs talk to a hardened (auth-on) gateway:
  - `data/register_dataset.py` registers a dataset when given the key (--api-key), and is rejected
    with a clear 401 when run with NO key against the auth-on gateway,
  - `monitoring/drift.py` runs a drift check with the key (a dataset vs itself -> no drift), and is
    likewise 401 without a key.

Requires the stack up AND a key (GATEWAY_API_KEY) — i.e. the auth-on posture this test is about; it
SKIPs otherwise (an open gateway has no auth to exercise here).

  python tests/test_cli_auth.py
"""
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.getenv("GATEWAY_API_KEY")


def _run(argv, with_key=True):
    """Run a repo CLI; when with_key is False, strip every key source from the child env."""
    env = dict(os.environ)
    if not with_key:
        for k in ("GATEWAY_API_KEY", "GATEWAY_API_KEYS_FILE"):
            env.pop(k, None)
    proc = subprocess.run([sys.executable, *argv], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    failures = 0

    # A tiny numeric CSV so the drift PSI computation has something to chew on.
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, dir=REPO) as f:
        f.write("x\n" + "\n".join(str(v) for v in range(20)) + "\n")
        csv_path = f.name
    rel_csv = os.path.relpath(csv_path, REPO)
    name = "cli-auth-demo"

    try:
        # 1. register WITH the key -> success; capture the version.
        rc, out, err = _run(["data/register_dataset.py", name, rel_csv, "--api-key", KEY])
        m = re.search(r"@ (\S+)", out)
        ok = rc == 0 and m is not None
        print(f"[{'OK' if ok else 'FAIL'}] register_dataset WITH key -> rc={rc} {out.strip()[:80]!r}")
        failures += 0 if ok else 1
        version = m.group(1) if m else None

        # 2. register WITHOUT a key against the auth-on gateway -> clear 401 failure.
        rc, out, err = _run(["data/register_dataset.py", name, rel_csv], with_key=False)
        blob = (out + err)
        ok = rc != 0 and "401" in blob
        print(f"[{'OK' if ok else 'FAIL'}] register_dataset NO key -> rc={rc} (expect non-zero + 401)")
        failures += 0 if ok else 1

        # 3. drift WITH the key (dataset vs itself -> no drift) -> success.
        if version:
            ref = f"{name}@{version}"
            rc, out, err = _run(["monitoring/drift.py", "--ref", ref, "--cur", ref, "--api-key", KEY])
            ok = rc == 0 and "drift" in out.lower()
            print(f"[{'OK' if ok else 'FAIL'}] drift WITH key -> rc={rc} {out.strip()[:80]!r}")
            failures += 0 if ok else 1

            # 4. drift WITHOUT a key -> 401.
            rc, out, err = _run(["monitoring/drift.py", "--ref", ref, "--cur", ref], with_key=False)
            blob = (out + err)
            ok = rc != 0 and "401" in blob
            print(f"[{'OK' if ok else 'FAIL'}] drift NO key -> rc={rc} (expect non-zero + 401)")
            failures += 0 if ok else 1
        else:
            print("[FAIL] could not parse a registered version — drift legs not exercised")
            failures += 1
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass

    if failures:
        print(f"\n{failures} CLI-auth check(s) failed.")
        return 1
    print("\nT098 PASS — dataset + drift CLIs authenticate with a key and 401 cleanly without one")
    return 0


def test_cli_auth(require_gateway, require_key):
    """Pytest wrapper (005 US5): needs a live, keyed gateway; else skip."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
