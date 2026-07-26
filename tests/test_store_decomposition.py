"""024 US1 (T562/T563) — the store decomposition: per-aggregate homes + driver-lazy imports.

Test-parity is the primary gate — the whole offline suite passes unchanged and `test_store_facade.py`
pins the public surface. These add the decomposition-specific pins:
  - each aggregate has its own `storeimpl/*` module, and the `store` facade re-exports THOSE exact
    functions (no accidental reimplementation drift);
  - the facade holds no aggregate SQL (import-only);
  - importing the object-store path never needs psycopg and importing the relational path never needs
    boto3 (FR-332 lazy drivers) — proven by importing with BOTH drivers blocked.
"""
import inspect
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from platformlib import s3io, store  # noqa: E402
from platformlib.storeimpl import (  # noqa: E402
    _engine,
    capture,
    jobs,
    labels,
    policies,
    predictions,
    serving_llm,
    suggestions,
)

# Each aggregate's home + the names the facade must re-export FROM it (unchanged for callers).
FACADE_HOMES = {
    predictions: ["log_prediction", "window", "prediction_exists"],
    labels: ["attach_label"],
    capture: ["capture_input", "capture_exists", "replay_window", "capture_rows",
              "delete_capture", "has_captures"],
    jobs: ["upsert_job", "import_job", "get_job", "list_jobs", "count_active_jobs",
           "mark_jobs_interrupted"],
    policies: ["put_policy", "get_policy", "list_policies", "delete_policy", "set_pending",
               "get_pending", "clear_pending", "set_status", "get_status"],
    suggestions: ["create_suggestion", "get_suggestion", "find_suggestion", "list_suggestions",
                  "resolve_suggestion"],
    serving_llm: ["set_serving_llm", "get_serving_llm", "clear_serving_llm"],
    _engine: ["dsn", "connect", "bootstrap", "ensure_schema"],
    s3io: ["s3_client", "list_keys", "list_common_prefixes"],
}


# --- per-aggregate homes + facade re-export identity -----------------------------------------------

def test_facade_reexports_are_the_storeimpl_functions():
    for module, names in FACADE_HOMES.items():
        for name in names:
            assert getattr(store, name) is getattr(module, name), \
                f"store.{name} is not {module.__name__}.{name} (facade re-export drifted)"


def test_serving_llm_and_shared_plumbing_left_the_facade():
    # T571: the serving_llm pointer SQL + the shared plumbing/constants relocated out of store.py.
    assert store.SCHEMA_VERSION is _engine.SCHEMA_VERSION
    assert store.TABLES is _engine.TABLES and "serving_llm" in store.TABLES
    # The facade holds NO aggregate SQL now — its source is import-only.
    src = inspect.getsource(store)
    assert "INSERT INTO" not in src and "SELECT " not in src


# --- driver-lazy imports (FR-332) ------------------------------------------------------------------

def test_relational_repos_do_not_import_boto3_at_module_scope():
    for module in (predictions, labels, capture, jobs, policies, suggestions, serving_llm, _engine):
        src = inspect.getsource(module)
        assert "\nimport boto3" not in src and "\nfrom boto3" not in src, \
            f"{module.__name__} imports boto3 at module scope"


def test_object_store_does_not_import_psycopg_at_module_scope():
    src = inspect.getsource(s3io)
    assert "\nimport psycopg" not in src and "\nfrom psycopg" not in src


def test_store_imports_with_both_drivers_blocked():
    """The load-bearing lazy-driver contract: importing the facade + every repo must NOT pull psycopg
    or boto3 at module scope, so the offline env / gateway image / native daemons load them
    driver-free. Proven in a clean interpreter with both drivers made unimportable."""
    code = (
        "import sys\n"
        "sys.modules['psycopg'] = None\n"   # a None entry makes `import psycopg` raise ImportError
        "sys.modules['boto3'] = None\n"
        "import platformlib.store as s\n"
        "import platformlib.s3io\n"
        "from platformlib.storeimpl import (predictions, labels, capture, jobs, policies,\n"
        "                                   suggestions, serving_llm, _engine)\n"
        "assert callable(s.log_prediction) and callable(s.s3_client) and callable(s.connect)\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
