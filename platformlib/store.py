"""`platformlib.store` — the public storage facade (024 US1, T571: reduced to re-exports).

Two sides, one public entry point ~24 call sites import as `from platformlib import store`:

  - **Object store**: the Garage/S3 client + paginated listings, now homed in `platformlib.s3io`
    (024 US1 consolidated them there rather than a second module) and re-exported here.
  - **Relational store** (contracts/store-schema.md): the high-churn monitoring state
    (predictions, labels, capture index, jobs, policies, suggestions, the serving-LLM pointer, and
    the activation-operation log), each lifted into its own `platformlib/storeimpl/*` repository and
    re-exported here. Shared connection/migration plumbing lives in `storeimpl/_engine`; the store
    errors + epoch/jsonb seam helpers in `storeimpl/_base`.

This module holds **no aggregate SQL** — it is a thin facade so every `store.<name>` call site resolves
unchanged (`tests/test_store_facade.py` pins the surface). Both drivers stay LAZY: boto3 is imported
inside the s3io factory and psycopg inside `connect()` / the write primitives, so importing `store`
(and thus this facade) never requires either — the native daemons and the offline env load it
driver-free. The failure posture is unchanged: prediction/capture WRITES fail-OPEN at the caller (the
`quality` wrapper drop-counts; the repo primitives PROPAGATE), label attach + window/policy/job READS
fail-LOUD.
"""
# -- store errors + epoch/jsonb seam helpers (storeimpl/_base) --------------------------------------
# -- object store: Garage/S3 client + paginated listings (platformlib.s3io) -------------------------
from platformlib.s3io import (  # noqa: F401 — re-exported (store.s3_client / store.list_keys / …)
    list_common_prefixes,
    list_keys,
    s3_client,
)
from platformlib.storeimpl._base import (  # noqa: F401 — re-exported (store.StoreError / store._json / …)
    LabelExists,
    StoreError,
    _dt,
    _epoch,
    _json,
)

# -- connection + schema/migration plumbing + shape constants (storeimpl/_engine) -------------------
from platformlib.storeimpl._engine import (  # noqa: F401 — re-exported
    SCHEMA_VERSION,
    TABLES,
    bootstrap,
    connect,
    dsn,
    ensure_schema,
)

# -- activation-operation repository (storeimpl/activations) ----------------------------------------
from platformlib.storeimpl.activations import (  # noqa: F401 — re-exported
    ACTIVATION_NONTERMINAL,
    ActivationConflict,
    cas_activation,
    create_activation,
    current_activation,
    find_activation_by_key,
    get_activation,
    list_resumable_activations,
)

# -- capture-index repository (storeimpl/capture) ---------------------------------------------------
from platformlib.storeimpl.capture import (  # noqa: F401 — re-exported
    capture_exists,
    capture_input,
    capture_rows,
    delete_capture,
    has_captures,
    replay_window,
)

# -- jobs repository (storeimpl/jobs) ---------------------------------------------------------------
from platformlib.storeimpl.jobs import (  # noqa: F401 — re-exported (_job_split/_job_row_to_record
    _job_row_to_record,  #   are reached by tests/_agentstore.py via the facade)
    _job_split,
    count_active_jobs,
    get_job,
    import_job,
    list_jobs,
    mark_jobs_interrupted,
    upsert_job,
)

# -- write-once labels repository (storeimpl/labels) ------------------------------------------------
from platformlib.storeimpl.labels import attach_label  # noqa: F401 — re-exported

# -- policy + pending + status repository (storeimpl/policies) --------------------------------------
from platformlib.storeimpl.policies import (  # noqa: F401 — re-exported
    clear_pending,
    delete_policy,
    get_pending,
    get_policy,
    get_status,
    list_policies,
    put_policy,
    set_pending,
    set_status,
)

# -- predictions repository + predictions⋈labels window (storeimpl/predictions) --------------------
from platformlib.storeimpl.predictions import (  # noqa: F401 — re-exported
    log_prediction,
    prediction_exists,
    window,
)

# -- serving-LLM pointer repository (storeimpl/serving_llm) -----------------------------------------
from platformlib.storeimpl.serving_llm import (  # noqa: F401 — re-exported
    clear_serving_llm,
    get_serving_llm,
    set_serving_llm,
)

# -- promotion-suggestions repository (storeimpl/suggestions) ---------------------------------------
from platformlib.storeimpl.suggestions import (  # noqa: F401 — re-exported
    create_suggestion,
    find_suggestion,
    get_suggestion,
    list_suggestions,
    resolve_suggestion,
)
