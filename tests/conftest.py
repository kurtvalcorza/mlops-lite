"""Shared pytest fixtures + skip guards for the integration suite (005 US5, FR-047/SC-029).

Converts the standalone ``tests/test_*.py`` scripts into a collectable pytest suite while preserving
their pass/skip semantics. Each module keeps its existing ``main() -> int`` (and ``__main__`` entry
for standalone use); a thin ``test_<name>()`` wrapper requests the right guard fixture(s) below and
asserts ``main() == 0``. The guards ``pytest.skip(...)`` when the live stack, an API key, a native
service, or WSL is absent — so ``pytest`` runs the whole suite against a live keyed stack and skips
cleanly offline (never a collect-nothing exit-5). No assertion logic changed; only structure + guards.

Key rotation note (FR-046): the gateway reads its key set once at startup, so a key change in .env or
the keys file takes effect only after the gateway restarts — these tests read GATEWAY_API_KEY the same
way (at run time) and assume the gateway was (re)started with the matching key.
"""
import json
import os
import shutil
import urllib.request

import pytest

GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8080")
UI_PORT = os.getenv("UI_PORT", "3000")
MLFLOW_PORT = os.getenv("MLFLOW_PORT", "5500")
GW = f"http://localhost:{GATEWAY_PORT}"
UI = f"http://127.0.0.1:{UI_PORT}"
MLFLOW = f"http://127.0.0.1:{MLFLOW_PORT}"


def _http_up(url, timeout=3.0) -> bool:
    """True if the URL answers with any HTTP status (reachable), False on connection error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError:
        return True  # answered (e.g. 401/404) => the service is up
    except Exception:
        return False


def _daemon_reachable(name: str) -> bool:
    """Read the native-daemon reachability from the gateway's OPEN /platform/health (no key needed)."""
    try:
        with urllib.request.urlopen(f"{GW}/platform/health", timeout=4) as r:
            data = json.load(r)
        return bool(data.get("daemons", {}).get(name, {}).get("reachable"))
    except Exception:
        return False


def _is_wsl() -> bool:
    """True on Linux/WSL (where the native daemons + supervisor can be driven)."""
    if os.name != "posix":
        return False
    try:
        return "microsoft" in open("/proc/version", encoding="utf-8", errors="ignore").read().lower()
    except Exception:
        return os.path.exists("/proc")  # any Linux is acceptable for the process-management tests


# --- value fixtures -------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def gateway_url() -> str:
    return GW


@pytest.fixture(scope="session")
def api_key():
    return os.getenv("GATEWAY_API_KEY")


# --- guard fixtures (skip when a prerequisite is absent) ------------------------------------------

@pytest.fixture
def require_gateway():
    if not _http_up(f"{GW}/healthz"):
        pytest.skip(f"gateway not reachable at {GW} — bring the stack up (up_all) to run this")


@pytest.fixture
def require_key():
    if not os.getenv("GATEWAY_API_KEY"):
        pytest.skip("GATEWAY_API_KEY not set — run scripts/gen_secrets and export it for auth checks")


@pytest.fixture
def require_serving(require_gateway):
    if not _daemon_reachable("serving"):
        pytest.skip("serving daemon not reachable — start the native llama-server (serve_up)")


@pytest.fixture
def require_vision(require_gateway):
    if not _daemon_reachable("vision"):
        pytest.skip("vision (BentoML) daemon not reachable — bash serving/bento/run.sh")


@pytest.fixture
def require_trainer(require_gateway):
    if not _daemon_reachable("training"):
        pytest.skip("training daemon not reachable — bash training/run.sh")


@pytest.fixture
def require_ui():
    if not _http_up(f"{UI}/healthz"):
        pytest.skip(f"operator console not reachable at {UI} — start it (ui/run.sh / up_all)")


@pytest.fixture
def require_wsl():
    if not _is_wsl():
        pytest.skip("not on Linux/WSL — native daemon/supervisor process management needs the WSL host")


@pytest.fixture
def require_node():
    if shutil.which("node") is None:
        pytest.skip("node not found — the portability/UI-tier contract assumes a provisioned box")


@pytest.fixture
def require_mlflow(require_gateway):
    if not _http_up(f"{MLFLOW}/health"):
        pytest.skip(f"MLflow not reachable at {MLFLOW} — inference-tracing tests need the MLflow server")


@pytest.fixture
def require_tracing(require_gateway):
    """Skip when the gateway reports tracing disabled (MLFLOW_TRACING_ENABLED=0)."""
    try:
        with urllib.request.urlopen(f"{GW}/", timeout=4) as r:
            if json.load(r).get("tracing") is not True:
                pytest.skip("gateway reports tracing disabled (MLFLOW_TRACING_ENABLED=0)")
    except Exception:
        pytest.skip("could not read the gateway tracing flag")
