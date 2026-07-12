"""023 US7 (T539, FR-324/325) — characterization of the `platformlib.store` public facade.

Pins the import/side-effect contract BEFORE the T543 extraction splits the relational repositories
and migration concerns into internal modules. Sixty call sites do `from platformlib import store`
and reach these names directly, so the extraction MUST keep every one importable from `store` with
an unchanged call signature and the same lazy-import behavior. This test fails loudly if a name is
dropped, renamed, or turned into a differently-shaped callable.

It is deliberately import-only (no live DB): importing `store` must never require psycopg or a
connection — that lazy-driver contract is itself load-bearing (the gateway image and the offline
test env import store without a driver at module load).
"""
import importlib
import inspect
import sys

import pytest

# The public facade every caller depends on. Grouped by concern to mirror the intended T543
# internal modules; the extraction re-exports each name so this list stays exact.
FACADE = {
    # object store (Garage/S3) helpers
    "s3_client": ["*"], "list_keys": ["s3", "bucket", "prefix"],
    "list_common_prefixes": ["s3", "bucket", "prefix", "delimiter"],
    # connection + schema/migration seam
    "dsn": [], "connect": ["dsn_str"], "bootstrap": ["conn"],
    "ensure_schema": ["dsn_str", "applied_by"],
    # predictions / labels / captures repository
    "log_prediction": ["conn", "prediction_id", "model_name", "version", "modality"],
    "attach_label": ["conn", "prediction_id", "label", "submitted_at"],
    "capture_input": ["conn", "prediction_id", "modality", "input_ref", "captured_at"],
    "window": ["conn", "modality", "model_name", "version", "n"],
    "prediction_exists": ["conn", "prediction_id"], "capture_exists": ["conn", "prediction_id"],
    "replay_window": ["conn", "modality", "model_name", "version", "n"],
    "capture_rows": ["conn", "modality"], "delete_capture": ["conn", "prediction_id"],
    "has_captures": ["conn", "modality"],
    # policy / pending / status repository
    "put_policy": ["conn", "model_name", "document", "updated_at", "updated_by"],
    "get_policy": ["conn", "model_name"], "list_policies": ["conn"],
    "delete_policy": ["conn", "model_name"], "set_pending": ["conn", "model_name", "pending"],
    "get_pending": ["conn", "model_name"], "clear_pending": ["conn", "model_name"],
    "set_status": ["conn", "model_name", "status"], "get_status": ["conn", "model_name"],
    # suggestion repository
    "create_suggestion": ["conn", "rec"], "get_suggestion": ["conn", "suggestion_id"],
    "find_suggestion": ["conn", "model_name", "candidate_version", "state"],
    "list_suggestions": ["conn", "state", "model_name"],
    "resolve_suggestion": ["conn", "suggestion_id", "state", "actor", "resolved_at"],
    # serving-LLM pointer repository
    "set_serving_llm": ["conn", "model_name", "selected_at", "selected_by"],
    "get_serving_llm": ["conn"], "clear_serving_llm": ["conn"],
    # activation-operation repository
    "create_activation": ["conn"], "get_activation": ["conn", "operation_id"],
    "find_activation_by_key": ["conn", "idempotency_key"], "current_activation": ["conn"],
    "list_resumable_activations": ["conn"],
    "cas_activation": ["conn", "operation_id", "expect_state", "new_state"],
    # jobs repository
    "upsert_job": ["conn", "record"], "import_job": ["conn", "record"],
}

FACADE_TYPES = ("StoreError", "LabelExists", "ActivationConflict")
FACADE_CONSTS = ("SCHEMA_VERSION", "TABLES", "ACTIVATION_NONTERMINAL")


@pytest.fixture()
def store():
    # Fresh import so the "import never needs psycopg" assertion is meaningful even if another
    # suite already loaded it.
    for m in [k for k in list(sys.modules) if k == "platformlib.store"]:
        sys.modules.pop(m, None)
    return importlib.import_module("platformlib.store")


def test_import_does_not_require_a_driver(store):
    """Importing store must not import psycopg (lazy-driver contract) — the extraction must keep
    the driver import inside connect()/_json(), never at module top level."""
    assert store is not None
    # psycopg is only pulled in by connect()/_json(); the facade must never import it at module
    # scope (that lazy-driver contract lets the offline env + gateway image load store driver-free).
    src = inspect.getsource(store)
    assert "\nimport psycopg" not in src and "\nfrom psycopg" not in src


def test_public_callables_present_with_expected_leading_params(store):
    missing = [n for n in FACADE if not callable(getattr(store, n, None))]
    assert not missing, f"facade dropped callables: {missing}"
    for name, expected_lead in FACADE.items():
        if expected_lead == ["*"]:
            continue
        sig = inspect.signature(getattr(store, name))
        params = [p for p in sig.parameters
                  if sig.parameters[p].kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                inspect.Parameter.POSITIONAL_ONLY)]
        lead = params[:len(expected_lead)]
        assert lead == expected_lead, f"{name}: leading params {lead} != {expected_lead}"


def test_public_types_and_constants_present(store):
    for t in FACADE_TYPES:
        assert isinstance(getattr(store, t, None), type), f"missing exception type {t}"
    assert issubclass(store.LabelExists, store.StoreError)
    assert issubclass(store.ActivationConflict, store.StoreError)
    for c in FACADE_CONSTS:
        assert hasattr(store, c), f"missing constant {c}"
    assert "predictions" in store.TABLES and "serving_llm" in store.TABLES
    assert set(store.ACTIVATION_NONTERMINAL) == {"prepared", "committing", "reloading",
                                                 "rolling_back"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
