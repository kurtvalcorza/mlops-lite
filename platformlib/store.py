"""Shared storage helpers (018 T343/T349 + US4 T373) — `platformlib.store`.

Two sides, one module:

  - **Object store** (T343/T349): the S3 access patterns that grew as private functions of
    `gateway/app/quality.py` and `gateway/app/datasets.py`, promoted to one shared home (review
    §4.5) — a cached client and **paginated** listings (no silent truncation past 1000 objects).
  - **Relational store** (US4, T373 — contracts/store-schema.md): the high-churn monitoring state
    (predictions, labels, capture index, jobs, policies, suggestions) moves off O(N) Garage object
    scans onto the resident Postgres `gateway` DB. One indexed predictions⋈labels window join
    replaces the per-request object listing (FR-186, SC-111): a window over 10k+ predictions
    resolves in well under the 5 s the object scan took.

Both drivers are imported LAZILY (boto3 in `s3_client()`, psycopg in `connect()`) so this module
stays importable in stdlib-only contexts — the native daemons import
`platformlib.topology`/`contracts` without needing either driver installed.

Relational design notes:
  - **Schema lives in ordered migrations** (023 US4): `platformlib/migrations/*.sql` via
    `platformlib.migrations` — the gateway applies at startup; `bootstrap()` here only VERIFIES
    compatibility before writes (the old embedded DDL + its init.sql mirror are retired, T513).
  - **Write-once labels (FR-185)** are enforced by the `labels` PRIMARY KEY, NOT an in-process lock:
    a second insert for the same `prediction_id` raises `LabelExists` (from a UniqueViolation), so
    concurrent duplicate submissions store exactly one label.
  - **Failure posture (FR-164):** the callers (013/016) fail-OPEN on prediction/label/capture WRITES
    (drop-counter, serving never blocks) and fail-LOUD on window/policy/job READS; this module just
    normalizes connection loss to `StoreError`.

Connection: `GATEWAY_DB_URL` (a full DSN) wins; else built from `POSTGRES_USER`/`POSTGRES_PASSWORD`
+ `GATEWAY_DB_HOST`/`GATEWAY_DB_PORT`/`GATEWAY_DB_NAME`. The gateway container reaches Postgres at
`postgres:5432` (compose injects `GATEWAY_DB_URL`); the native WSL host agent reaches it at the
host-published `127.0.0.1:$POSTGRES_PORT` — `hostagent/run.sh` sets `GATEWAY_DB_HOST`/`GATEWAY_DB_PORT`
for that (T375/T377), since the in-container `postgres` hostname is unresolvable from WSL.
"""
import os
import threading

_client = None
_client_lock = threading.Lock()


def s3_client():
    """A cached S3 client (env-configured, credentials required — FR-017 fail-loud).

    Reuses one client per process; boto3 clients are thread-safe for use (creation is not,
    hence the lock)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.client import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=os.getenv("S3_ENDPOINT_URL")
                    or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://garage:3900"),
                    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                    config=Config(signature_version="s3v4"),
                )
    return _client


def list_keys(s3, bucket: str, prefix: str) -> list:
    """All object keys under `prefix`, paginating past the 1000-object `list_objects_v2` page cap
    (FR-165). A truncated single page silently windows an arbitrary slice — never acceptable for
    operator-facing listings or monitoring windows."""
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")
        if not token:
            return keys


def list_common_prefixes(s3, bucket: str, prefix: str = "", delimiter: str = "/") -> list:
    """All CommonPrefixes under `prefix` (delimiter listings paginate too — the dataset registry's
    name/version listings truncated past 1000 entries before 018, FR-165)."""
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "Delimiter": delimiter}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        out.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")
        if not token:
            return out


# ==================================================================================================
# Relational store (US4, T373) — contracts/store-schema.md
# ==================================================================================================

#: Kept for backward compatibility with the pre-023 meta seed (001_baseline.sql seeds the same 1).
SCHEMA_VERSION = 1

#: The tables the schema must present — used by tests and shape checks. The DDL itself moved to
#: `platformlib/migrations/001_baseline.sql` (023 US4, FR-297): ordered migration files are the
#: ONLY schema authority now; the embedded full-schema DDL string and its init.sql mirror are gone.
TABLES = ("meta", "predictions", "labels", "capture_index", "jobs", "policies", "suggestions",
          "serving_llm")


class StoreError(Exception):
    """The store is unreachable / a read failed. Callers map reads to 502; writes fail-open."""


class LabelExists(StoreError):
    """A label for this prediction_id already exists — the write-once constraint (FR-185)."""


def dsn() -> str:
    """The Postgres DSN for the `gateway` DB. `GATEWAY_DB_URL` (full DSN) wins; else assembled from
    the POSTGRES_* creds + GATEWAY_DB_HOST/PORT/NAME (defaults suit the gateway container)."""
    url = os.getenv("GATEWAY_DB_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "mlops")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("GATEWAY_DB_HOST", "postgres")
    port = os.getenv("GATEWAY_DB_PORT", "5432")
    name = os.getenv("GATEWAY_DB_NAME", "gateway")
    auth = f"{user}:{pw}@" if pw else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{name}"


def connect(dsn_str: str = None, *, autocommit: bool = True):
    """A psycopg connection to the gateway DB. psycopg is imported here (lazily) so importing this
    module never requires the driver. Raises StoreError on connection failure so callers can apply
    the fail-open (writes) / fail-loud (reads) posture without catching psycopg types."""
    try:
        import psycopg
    except ModuleNotFoundError as e:  # pragma: no cover - environment-dependent
        raise StoreError("psycopg is not installed — install psycopg[binary]") from e
    try:
        return psycopg.connect(dsn_str or dsn(), autocommit=autocommit)
    except Exception as e:  # noqa: BLE001 — normalize every driver/connection error to StoreError
        raise StoreError(f"cannot connect to the gateway DB: {e}") from e


def bootstrap(conn=None) -> None:
    """Verify the schema is COMPATIBLE (023 US4, FR-299/301) — this no longer applies DDL.

    Ordered migrations own evolution now: the GATEWAY applies them at startup
    (gateway/app/main.py); every other store client (host-agent journal, tools, ad-hoc writers)
    calls this before writing and fails closed on an unledgered/older/newer schema instead of
    racing its own `CREATE IF NOT EXISTS` against a live database. The name survives because
    every pre-023 client init already calls it at exactly the right moment."""
    from platformlib import migrations
    close = conn is None
    conn = conn or connect()
    try:
        try:
            migrations.check_compatible(conn)
        except migrations.MigrationError as e:
            raise StoreError(str(e)) from e
    finally:
        if close:
            conn.close()


def ensure_schema(dsn_str: str = None, applied_by: str = "tool") -> dict:
    """Apply pending migrations (the gateway-startup/CLI/test entry point — NOT for the agent's
    write path, which only checks). Returns the migrations report."""
    from platformlib import migrations
    try:
        return migrations.apply(dsn=dsn_str or dsn(), applied_by=applied_by, log=lambda *a: None)
    except migrations.MigrationError as e:
        raise StoreError(str(e)) from e


def _json(value):
    """Wrap a Python value for a jsonb column (psycopg needs an explicit adapter for dict/list)."""
    from psycopg.types.json import Json
    return Json(value)


# -- writes (fail-open at the caller; ON CONFLICT DO NOTHING keeps re-issues idempotent) -----------

def log_prediction(conn, prediction_id: str, model_name: str, version: str, modality: str,
                   served_at, *, streamed: bool = False, payload_ref: str = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO predictions "
            "(prediction_id, model_name, version, modality, served_at, streamed, payload_ref) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (prediction_id) DO NOTHING",
            (prediction_id, model_name, version, modality, served_at, streamed, payload_ref))


def attach_label(conn, prediction_id: str, label, submitted_at) -> None:
    """Insert a label, enforcing write-once via the PRIMARY KEY (FR-185). A duplicate raises
    LabelExists — the concurrent-safe replacement for the retired in-process `_label_write_lock`."""
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO labels (prediction_id, label, submitted_at) VALUES (%s, %s, %s)",
                (prediction_id, _json(label), submitted_at))
    except psycopg.errors.UniqueViolation as e:
        raise LabelExists(f"label already stored for {prediction_id}") from e


def capture_input(conn, prediction_id: str, modality: str, input_ref: str, captured_at) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capture_index (prediction_id, modality, input_ref, captured_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (prediction_id) DO NOTHING",
            (prediction_id, modality, input_ref, captured_at))


# -- reads (fail loud at the caller) ---------------------------------------------------------------

def window(conn, modality: str, model_name: str, version: str, n: int) -> list:
    """The `served_at DESC LIMIT n` predictions⋈labels join that replaces the O(N) object scan
    (FR-186, SC-111). Returns the most recent `n` LABELED predictions for the (modality, model,
    version) as dicts {prediction_id, served_at, payload_ref, label, submitted_at}. The composite
    index `ix_pred_window` serves the ordering, so this is a bounded index scan, not a listing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.prediction_id, p.served_at, p.payload_ref, l.label, l.submitted_at "
            "FROM predictions p JOIN labels l USING (prediction_id) "
            "WHERE p.modality = %s AND p.model_name = %s AND p.version = %s "
            "ORDER BY p.served_at DESC LIMIT %s",
            (modality, model_name, version, n))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def prediction_exists(conn, prediction_id: str) -> bool:
    """Whether a prediction row was logged (013 attach_label's 'unknown id' check — the write-once
    label FK also guards it, but this yields the explicit `unknown` status without an FK error)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM predictions WHERE prediction_id = %s", (prediction_id,))
        return cur.fetchone() is not None


def capture_exists(conn, prediction_id: str) -> bool:
    """Whether a capture-index row exists for this prediction (symmetric with `prediction_exists`) —
    the backfill's idempotency precheck so a re-run counts an already-migrated input as skipped."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM capture_index WHERE prediction_id = %s", (prediction_id,))
        return cur.fetchone() is not None


def replay_window(conn, modality: str, model_name: str, version: str, n: int,
                  *, ttl_cutoff=None) -> list:
    """The shadow-replay (016) resolution path: the most recent `n` captured∩labeled predictions for
    (modality, model, version), newest first, as dicts {prediction_id, input_ref, payload_ref, label,
    captured_at}. `input_ref` locates the recoverable served input (for re-running the challenger);
    `payload_ref` the champion's logged output. `ttl_cutoff` (a timestamptz) drops captures older than
    the retention window IN THE QUERY (FR-186) — no per-object key parse. Indexed by `capture_index`
    PK + the predictions window index; bounded gets follow on only these `n` rows."""
    sql = ("SELECT c.prediction_id, c.input_ref, p.payload_ref, l.label, c.captured_at "
           "FROM capture_index c "
           "JOIN predictions p USING (prediction_id) "
           "JOIN labels l USING (prediction_id) "
           "WHERE c.modality = %s AND p.model_name = %s AND p.version = %s")
    params = [modality, model_name, version]
    if ttl_cutoff is not None:
        sql += " AND c.captured_at >= %s"
        params.append(ttl_cutoff)
    sql += " ORDER BY c.captured_at DESC LIMIT %s"
    params.append(n)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        cols = [col.name for col in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def capture_rows(conn, modality: str) -> list:
    """All `(prediction_id, input_ref, captured_at)` capture-index rows for a modality — the input to
    the cap/TTL prune (the successor to listing `inputs/<modality>/` objects)."""
    with conn.cursor() as cur:
        cur.execute("SELECT prediction_id, input_ref, captured_at FROM capture_index "
                    "WHERE modality = %s", (modality,))
        return [{"prediction_id": r[0], "input_ref": r[1], "captured_at": r[2]}
                for r in cur.fetchall()]


def delete_capture(conn, prediction_id: str) -> None:
    """Drop a capture-index row (the object body is deleted separately) — used by the prune."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_index WHERE prediction_id = %s", (prediction_id,))


def has_captures(conn, modality: str) -> bool:
    """Whether ANY recoverable input is captured for `modality` (shadow-replay corpus presence)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM capture_index WHERE modality = %s LIMIT 1", (modality,))
        return cur.fetchone() is not None


# ==================================================================================================
# US3 policy state (US4 T375) — policies + queue-of-one pending retrain + check status + suggestions
# ==================================================================================================
#
# The pre-US4 policy state rode Garage objects (policies/*.json + policies/_pending/ + policies/_status/
# + suggestions/*.json); `list_policies`/`list_suggestions` LISTED + read every object (the O(N) scan
# SC-111 kills). Here they become indexed table reads. Timestamps in the records stay epoch FLOATS (the
# UI + the pre-US4 shape) — the timestamptz columns exist for ordering; the helpers convert at the seam.


def _dt(epoch):
    """Epoch float (or None) -> a tz-aware datetime for a timestamptz column."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None


def _epoch(dt):
    """A timestamptz value (or None) -> epoch float, restoring the record's numeric timestamp."""
    return dt.timestamp() if dt is not None else None


# -- policies (FR-179) -----------------------------------------------------------------------------

def put_policy(conn, model_name: str, document: dict, updated_at, updated_by: str) -> None:
    """Upsert the validated ModelPolicy `document`. ON CONFLICT touches only document/updated_at/
    updated_by — a re-declaration must NOT wipe the row's pending/status (they outlive a re-`put`,
    matching the pre-US4 separate-object behavior)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO policies (model_name, document, updated_at, updated_by) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (model_name) DO UPDATE SET "
            "document = EXCLUDED.document, updated_at = EXCLUDED.updated_at, "
            "updated_by = EXCLUDED.updated_by",
            (model_name, _json(document), _dt(updated_at), updated_by))


def get_policy(conn, model_name: str):
    """The stored ModelPolicy document (epoch-float timestamps intact), or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT document FROM policies WHERE model_name = %s", (model_name,))
        row = cur.fetchone()
        return row[0] if row else None


def list_policies(conn) -> list:
    """Every policy document, name-ordered — one indexed scan of the `policies` table (no O(N) object
    listing, no `_pending`/`_status` sub-prefix filtering)."""
    with conn.cursor() as cur:
        cur.execute("SELECT document FROM policies ORDER BY model_name")
        return [r[0] for r in cur.fetchall()]


def delete_policy(conn, model_name: str) -> None:
    """Drop the policy row — its pending + status columns go with it (no separate deletes)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM policies WHERE model_name = %s", (model_name,))


# -- queue-of-one pending retrain + per-model check status (columns on the policy row) --------------

def set_pending(conn, model_name: str, pending: dict) -> None:
    """Park the queue-of-one retrain on the policy row. UPDATE-only: a pending for a policy that no
    longer exists is meaningless (the pre-US4 orphan `_pending` object can't happen)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE policies SET pending = %s WHERE model_name = %s",
                    (_json(pending), model_name))


def get_pending(conn, model_name: str):
    with conn.cursor() as cur:
        cur.execute("SELECT pending FROM policies WHERE model_name = %s", (model_name,))
        row = cur.fetchone()
        return row[0] if row else None  # None both when no policy and when nothing is parked


def clear_pending(conn, model_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE policies SET pending = NULL WHERE model_name = %s", (model_name,))


def set_status(conn, model_name: str, status: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE policies SET status = %s WHERE model_name = %s",
                    (_json(status), model_name))


def get_status(conn, model_name: str):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM policies WHERE model_name = %s", (model_name,))
        row = cur.fetchone()
        return row[0] if row else None


# -- suggestions + audit (FR-183) ------------------------------------------------------------------

_SUGGESTION_COLS = ("id", "model_name", "candidate_version", "gate_verdict", "shadow_verdict",
                    "state", "created_at", "resolved_at", "actor")


def _suggestion_row(row) -> dict:
    """A suggestions row -> the record dict, restoring created_at/resolved_at to epoch floats."""
    d = dict(zip(_SUGGESTION_COLS, row))
    d["created_at"] = _epoch(d["created_at"])
    d["resolved_at"] = _epoch(d["resolved_at"])
    return d


def create_suggestion(conn, rec: dict) -> None:
    """Insert a PromotionSuggestion record (its id is the caller-minted PK)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO suggestions (id, model_name, candidate_version, gate_verdict, "
            "shadow_verdict, state, created_at, resolved_at, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (rec["id"], rec["model_name"], str(rec["candidate_version"]),
             _json(rec.get("gate_verdict") or {}),
             _json(rec["shadow_verdict"]) if rec.get("shadow_verdict") is not None else None,
             rec["state"], _dt(rec.get("created_at")), _dt(rec.get("resolved_at")), rec.get("actor")))


def get_suggestion(conn, suggestion_id: str):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_SUGGESTION_COLS)} FROM suggestions WHERE id = %s",
                    (suggestion_id,))
        row = cur.fetchone()
        return _suggestion_row(row) if row else None


def find_suggestion(conn, model_name: str, candidate_version: str, state: str):
    """The suggestion for (model, candidate, state), or None — backs create_suggestion's idempotency
    (per-(model,version,state): a re-run must not mint a duplicate open suggestion OR a duplicate
    auto-promoted audit row for the same candidate)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_SUGGESTION_COLS)} FROM suggestions "
            "WHERE model_name = %s AND candidate_version = %s AND state = %s LIMIT 1",
            (model_name, str(candidate_version), state))
        row = cur.fetchone()
        return _suggestion_row(row) if row else None


def list_suggestions(conn, state: str = None, model_name: str = None) -> list:
    """Suggestions newest-first, optionally filtered by state and/or model — an indexed table scan,
    not a listing of every `suggestions/` object."""
    sql = f"SELECT {', '.join(_SUGGESTION_COLS)} FROM suggestions"
    where, params = [], []
    if state is not None:
        where.append("state = %s")
        params.append(state)
    if model_name is not None:
        where.append("model_name = %s")
        params.append(model_name)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [_suggestion_row(r) for r in cur.fetchall()]


def resolve_suggestion(conn, suggestion_id: str, state: str, actor: str, resolved_at):
    """Transition an OPEN suggestion to a terminal `state`. The `state = 'open'` guard makes the
    accept/dismiss race atomic in the DB (a lost race updates 0 rows). Returns the updated record, or
    None if the id doesn't exist / was already resolved (the caller maps None → 404-or-conflict)."""
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE suggestions SET state = %s, resolved_at = %s, actor = %s "
            f"WHERE id = %s AND state = 'open' RETURNING {', '.join(_SUGGESTION_COLS)}",
            (state, _dt(resolved_at), actor, suggestion_id))
        row = cur.fetchone()
        return _suggestion_row(row) if row else None


# ==================================================================================================
# 022 T461 — the ActiveServingLLM pointer (data-model.md §ActiveServingLLM)
# ==================================================================================================
#
# One platform-scoped row naming the active text-generation model. Written by the gateway's gated
# promote (022 US1 — promote = go live); read by the host agent's cold-load resolver
# (hostagent/serving_llm.py). Unset ⇒ the configured default base serves (today's behavior), so a
# fresh store changes nothing until an operator promotes an LLM. Timestamps follow the house rule:
# epoch floats at the seam, timestamptz in the column.


def set_serving_llm(conn, model_name: str, selected_at, selected_by: str) -> None:
    """Point the active serving-LLM at `model_name` (upsert — the singleton PK keeps it one row)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO serving_llm (singleton, model_name, selected_at, selected_by) "
            "VALUES (true, %s, %s, %s) ON CONFLICT (singleton) DO UPDATE SET "
            "model_name = EXCLUDED.model_name, selected_at = EXCLUDED.selected_at, "
            "selected_by = EXCLUDED.selected_by",
            (model_name, _dt(selected_at), selected_by))


def get_serving_llm(conn):
    """The active serving-LLM selection {model_name, selected_at, selected_by}, or None when unset
    (⇒ the configured default base serves)."""
    with conn.cursor() as cur:
        cur.execute("SELECT model_name, selected_at, selected_by FROM serving_llm")
        row = cur.fetchone()
        if row is None:
            return None
        return {"model_name": row[0], "selected_at": _epoch(row[1]), "selected_by": row[2]}


def clear_serving_llm(conn) -> None:
    """Unset the pointer — back to the configured default base (tests/reset; no operator surface)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM serving_llm")


# ==================================================================================================
# 023 US5 (T520) — ActivationOperation repository (contracts/promotion-activation.md)
# ==================================================================================================
#
# One operator go-live action, made recoverable. Every transition is a COMPARE-AND-SET on `state`
# (a lost race updates 0 rows and returns None); the partial unique indexes in 002_activation.sql
# enforce one non-terminal operation platform-wide and idempotency-key uniqueness among them.
# Timestamps follow the house rule (epoch floats at the seam, timestamptz in the column); the
# `evidence` jsonb carries sanitized authority observations only — never secrets/paths/stacks.

#: Non-terminal states — the reconciler resumes these (degraded is visible but terminal-manual).
ACTIVATION_NONTERMINAL = ("prepared", "committing", "reloading", "rolling_back")

_ACTIVATION_COLS = ("operation_id", "idempotency_key", "state", "actor", "target_model",
                    "target_version", "previous_model", "previous_version", "attempts",
                    "created_at", "updated_at", "last_error_code", "last_error", "evidence")


class ActivationConflict(StoreError):
    """Another non-terminal activation exists (or an idempotency-key collision) — the caller maps
    this to 409, never a duplicate reload."""


def _activation_row(row) -> dict:
    d = dict(zip(_ACTIVATION_COLS, row))
    d["created_at"] = _epoch(d["created_at"])
    d["updated_at"] = _epoch(d["updated_at"])
    d["evidence"] = d["evidence"] or {}
    return d


def create_activation(conn, *, operation_id: str, idempotency_key: str, actor: str,
                      target_model: str, target_version: str, previous_model=None,
                      previous_version=None, evidence: dict = None) -> dict:
    import psycopg
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO activation_operations (operation_id, idempotency_key, state, actor, "
                "target_model, target_version, previous_model, previous_version, evidence) "
                "VALUES (%s, %s, 'prepared', %s, %s, %s, %s, %s, %s) "
                f"RETURNING {', '.join(_ACTIVATION_COLS)}",
                (operation_id, idempotency_key, actor, target_model, str(target_version),
                 previous_model, str(previous_version) if previous_version is not None else None,
                 _json(evidence or {})))
            return _activation_row(cur.fetchone())
    except psycopg.errors.UniqueViolation as e:
        raise ActivationConflict(
            "another activation is already in progress (one non-terminal operation platform-wide)"
        ) from e


def get_activation(conn, operation_id: str):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_ACTIVATION_COLS)} FROM activation_operations "
                    "WHERE operation_id = %s", (operation_id,))
        row = cur.fetchone()
        return _activation_row(row) if row else None


def find_activation_by_key(conn, idempotency_key: str):
    """Newest operation for this idempotency key (non-terminal first, then most recent)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_ACTIVATION_COLS)} FROM activation_operations "
            "WHERE idempotency_key = %s "
            "ORDER BY (state = ANY(%s)) DESC, created_at DESC LIMIT 1",
            (idempotency_key, list(ACTIVATION_NONTERMINAL)))
        row = cur.fetchone()
        return _activation_row(row) if row else None


def current_activation(conn):
    """The single non-terminal operation, or the most recent terminal one (for the read model)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_ACTIVATION_COLS)} FROM activation_operations "
            "ORDER BY (state = ANY(%s)) DESC, created_at DESC LIMIT 1",
            (list(ACTIVATION_NONTERMINAL),))
        row = cur.fetchone()
        return _activation_row(row) if row else None


def list_resumable_activations(conn) -> list:
    """Every non-terminal operation (the startup/periodic reconciler's work queue)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_ACTIVATION_COLS)} FROM activation_operations "
                    "WHERE state = ANY(%s) ORDER BY created_at",
                    (list(ACTIVATION_NONTERMINAL),))
        return [_activation_row(r) for r in cur.fetchall()]


def cas_activation(conn, operation_id: str, expect_state, new_state: str, *,
                   error_code=None, error=None, evidence_patch: dict = None,
                   bump_attempts: bool = False):
    """Compare-and-set transition: succeeds only from `expect_state` (str or tuple). Returns the
    updated record, or None when the operation moved concurrently (the caller re-reads and defers
    to whoever won). `evidence_patch` merges (never replaces) the observation object."""
    expect = [expect_state] if isinstance(expect_state, str) else list(expect_state)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE activation_operations SET state = %s, updated_at = now(), "
            "last_error_code = %s, last_error = %s, "
            "evidence = evidence || %s::jsonb, "
            "attempts = attempts + %s "
            "WHERE operation_id = %s AND state = ANY(%s) "
            f"RETURNING {', '.join(_ACTIVATION_COLS)}",
            (new_state, error_code, (error or "")[:500] or None, _json(evidence_patch or {}),
             1 if bump_attempts else 0, operation_id, expect))
        row = cur.fetchone()
        return _activation_row(row) if row else None


# ==================================================================================================
# US2 job records (US4 T375-B) — the durable `jobs` table behind the host-agent journal
# ==================================================================================================
#
# Pre-US4 the agent journalled one JSONL line per state transition and folded the file at startup
# (hostagent/journal.py). Here the `jobs` table IS that durable log: the agent keeps its
# authoritative in-memory job table and writes durable-first to Postgres (an upsert per transition),
# hydrating the table from `list_jobs` on start and flipping crash-orphaned jobs with one atomic
# `mark_jobs_interrupted`. The flat JobRecord bag (data-model.md §JobRecord) maps to the typed
# columns job_id/kind/modality/state + the three timestamps + `request`, and a `result` jsonb
# CATCH-ALL that carries every OTHER record key — the kind-specific outputs
# (mlflow_run_id/model/metrics/summary/best/error), the failure `reason`, and the legacy id_key
# alias (run_id/study_id/…) — so a record round-trips losslessly. Record timestamps are epoch floats
# (the trainer/UI shape); the columns are timestamptz for ordering; `_dt`/`_epoch` convert at the seam.

_JOB_RESERVED = ("job_id", "kind", "modality", "request", "state",
                 "submitted_at", "started_at", "ended_at")

_JOB_SELECT = ("SELECT job_id, kind, modality, request, state, submitted_at, started_at, "
               "ended_at, result FROM jobs")


def _job_split(record: dict):
    """A flat JobRecord bag -> the column tuple. Every key that is NOT a typed column or `request`
    lands in the `result` catch-all, so kind-specific fields + `reason` + the id_key alias all
    survive the round-trip (the DB has no column for them individually)."""
    catch = {k: v for k, v in record.items() if k not in _JOB_RESERVED}
    return (record["job_id"], record.get("kind") or "", record.get("modality") or "",
            record.get("request") or {}, record.get("state") or "queued",
            record.get("submitted_at"), record.get("started_at"), record.get("ended_at"), catch)


def _job_row_to_record(row) -> dict:
    """A jobs row -> the flat JobRecord dict. started_at/ended_at are set only when present (the
    pre-US4 record omits them until a transition sets them) and the `result` catch-all is spread back
    to the top level — reproducing the journalled record shape exactly (byte-compat, FR-177)."""
    (job_id, kind, modality, request, state, submitted_at, started_at, ended_at, result) = row
    rec = {"job_id": job_id, "kind": kind, "modality": modality, "request": request or {},
           "state": state, "submitted_at": _epoch(submitted_at)}
    if started_at is not None:
        rec["started_at"] = _epoch(started_at)
    if ended_at is not None:
        rec["ended_at"] = _epoch(ended_at)
    if result:
        rec.update(result)  # catch-all: reason + kind-specific outputs + the id_key alias
    return rec


def _job_insert(conn, record: dict, on_conflict: str) -> int:
    jid, kind, modality, request, state, sub, start, end, catch = _job_split(record)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (job_id, kind, modality, request, state, submitted_at, "
            "started_at, ended_at, result) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            + on_conflict,
            (jid, kind, modality, _json(request), state, _dt(sub), _dt(start), _dt(end),
             _json(catch) if catch else None))
        return cur.rowcount


def upsert_job(conn, record: dict) -> None:
    """Durable-first write of the FULL record (INSERT ... ON CONFLICT DO UPDATE). The agent calls this
    BEFORE advancing its in-memory table, so a failed write never leaves memory ahead of the store —
    the JSONL fsync-before-memory guarantee (FR-189), now a committed row."""
    _job_insert(conn, record,
                "ON CONFLICT (job_id) DO UPDATE SET kind = EXCLUDED.kind, "
                "modality = EXCLUDED.modality, request = EXCLUDED.request, state = EXCLUDED.state, "
                "submitted_at = EXCLUDED.submitted_at, started_at = EXCLUDED.started_at, "
                "ended_at = EXCLUDED.ended_at, result = EXCLUDED.result")


def import_job(conn, record: dict) -> bool:
    """Backfill insert (T376): the same split, but ON CONFLICT DO NOTHING (an already-migrated job is
    left untouched). Returns True iff a row was inserted (for the migration's counts)."""
    return _job_insert(conn, record, "ON CONFLICT (job_id) DO NOTHING") > 0


def get_job(conn, job_id: str):
    with conn.cursor() as cur:
        cur.execute(_JOB_SELECT + " WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        return _job_row_to_record(row) if row else None


def list_jobs(conn, kind: str = None) -> list:
    """Every job record, newest-first (optionally one kind) — the `ix_jobs_kind` index serves the
    (kind, submitted_at DESC) ordering. Hydrates the agent's in-memory table on start."""
    sql = _JOB_SELECT
    params = ()
    if kind is not None:
        sql += " WHERE kind = %s"
        params = (kind,)
    sql += " ORDER BY submitted_at DESC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_job_row_to_record(r) for r in cur.fetchall()]


def count_active_jobs(conn) -> int:
    """Jobs still queued or running — the `hostagent_jobs_active` gauge + the /health field."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')")
        return cur.fetchone()[0]


def mark_jobs_interrupted(conn, reason: str, ended_at) -> list:
    """Flip every still-queued/running job to `interrupted` in ONE atomic UPDATE at agent startup
    (FR-173): a crash killed them with the previous agent. Returns the flipped job_ids (for the alert
    count + the in-memory table update). `reason` folds into the `result` catch-all (merged, not
    clobbering any partial result); `ended_at` sets the column."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET state = 'interrupted', ended_at = %s, "
            # cast the bound param to jsonb: _json binds as `json`, and `jsonb || json` has no operator
            "result = coalesce(result, '{}'::jsonb) || %s::jsonb "
            "WHERE state IN ('queued', 'running') RETURNING job_id",
            (_dt(ended_at), _json({"reason": reason})))
        return [r[0] for r in cur.fetchall()]
