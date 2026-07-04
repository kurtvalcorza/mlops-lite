"""Shared storage helpers (018 T343/T349 + US4 T373) — `platformlib.store`.

Two sides, one module:

  - **Object store** (T343/T349): the S3 access patterns that grew as private functions of
    `gateway/app/quality.py` and `gateway/app/datasets.py`, promoted to one shared home (review
    §4.5) — a cached client and **paginated** listings (no silent truncation past 1000 objects).
  - **Relational store** (US4, T373 — contracts/store-schema.md): the high-churn monitoring state
    (predictions, labels, capture index, jobs, policies, suggestions) moves off O(N) MinIO object
    scans onto the resident Postgres `gateway` DB. One indexed predictions⋈labels window join
    replaces the per-request object listing (FR-186, SC-111): a window over 10k+ predictions
    resolves in well under the 5 s the object scan took.

Both drivers are imported LAZILY (boto3 in `s3_client()`, psycopg in `connect()`) so this module
stays importable in stdlib-only contexts — the native daemons import
`platformlib.topology`/`contracts` without needing either driver installed.

Relational design notes:
  - **bootstrap() is idempotent + additive-only** (`CREATE ... IF NOT EXISTS`), mirrored in
    `infra/postgres/init.sql` for a fresh volume; a single `meta(schema_version)` row tracks it.
  - **Write-once labels (FR-185)** are enforced by the `labels` PRIMARY KEY, NOT an in-process lock:
    a second insert for the same `prediction_id` raises `LabelExists` (from a UniqueViolation), so
    concurrent duplicate submissions store exactly one label.
  - **Failure posture (FR-164):** the callers (013/016) fail-OPEN on prediction/label/capture WRITES
    (drop-counter, serving never blocks) and fail-LOUD on window/policy/job READS; this module just
    normalizes connection loss to `StoreError`.

Connection: `GATEWAY_DB_URL` (a full DSN) wins; else built from `POSTGRES_USER`/`POSTGRES_PASSWORD`
+ `GATEWAY_DB_HOST`/`GATEWAY_DB_PORT`/`GATEWAY_DB_NAME`. The gateway container reaches Postgres at
`postgres:5432`; the native WSL host (agent, tests) at `127.0.0.1:55432` — so the compose file and
`up_all.ps1` each inject the context-correct `GATEWAY_DB_URL`.
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
                    or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000"),
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

#: Bumped only on a non-additive schema change (none within 018). `bootstrap()` seeds `meta`.
SCHEMA_VERSION = 1

#: The full schema (contracts/store-schema.md). Additive-only; every statement is IF NOT EXISTS so
#: bootstrap() is safe on every client init and the infra/postgres/init.sql mirror can't drift.
DDL = """
CREATE TABLE IF NOT EXISTS meta (schema_version int NOT NULL);

CREATE TABLE IF NOT EXISTS predictions (
  prediction_id text PRIMARY KEY,
  model_name    text NOT NULL,
  version       text NOT NULL,
  modality      text NOT NULL,
  served_at     timestamptz NOT NULL,
  streamed      boolean NOT NULL DEFAULT false,
  payload_ref   text
);
CREATE INDEX IF NOT EXISTS ix_pred_window
  ON predictions (modality, model_name, version, served_at DESC);

CREATE TABLE IF NOT EXISTS labels (
  prediction_id text PRIMARY KEY REFERENCES predictions(prediction_id),
  label         jsonb NOT NULL,
  submitted_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_index (
  prediction_id text PRIMARY KEY,
  modality      text NOT NULL,
  input_ref     text NOT NULL,
  captured_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id       text PRIMARY KEY,
  kind         text NOT NULL,
  modality     text NOT NULL,
  request      jsonb NOT NULL,
  state        text NOT NULL,
  submitted_at timestamptz NOT NULL,
  started_at   timestamptz,
  ended_at     timestamptz,
  result       jsonb
);
CREATE INDEX IF NOT EXISTS ix_jobs_kind ON jobs (kind, submitted_at DESC);

CREATE TABLE IF NOT EXISTS policies (
  model_name text PRIMARY KEY,
  document   jsonb NOT NULL,
  updated_at timestamptz NOT NULL,
  updated_by text NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
  id                text PRIMARY KEY,
  model_name        text NOT NULL,
  candidate_version text NOT NULL,
  gate_verdict      jsonb NOT NULL,
  shadow_verdict    jsonb,
  state             text NOT NULL,
  created_at        timestamptz NOT NULL,
  resolved_at       timestamptz,
  actor             text
);
"""

#: The tables bootstrap() must create — used by the test + a post-bootstrap self-check.
TABLES = ("meta", "predictions", "labels", "capture_index", "jobs", "policies", "suggestions")


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
    """Apply the schema idempotently and seed `meta`. Safe to call on every client init (all DDL is
    IF NOT EXISTS). Opens its own connection when `conn` is None."""
    close = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(
                "INSERT INTO meta (schema_version) SELECT %s "
                "WHERE NOT EXISTS (SELECT 1 FROM meta)", (SCHEMA_VERSION,))
        if not getattr(conn, "autocommit", True):
            conn.commit()
    finally:
        if close:
            conn.close()


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
