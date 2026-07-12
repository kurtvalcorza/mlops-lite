"""023 US5 (T520) — ActivationOperation repository (contracts/promotion-activation.md).

One operator go-live action, made recoverable. Every transition is a COMPARE-AND-SET on `state`
(a lost race updates 0 rows and returns None); the partial unique indexes in 002_activation.sql
enforce one non-terminal operation platform-wide and idempotency-key uniqueness among them.
Timestamps follow the house rule (epoch floats at the seam, timestamptz in the column); the
`evidence` jsonb carries sanitized authority observations only — never secrets/paths/stacks.

Extracted from `store.py` (023 US7 T543) behind the `store` facade — call sites are unchanged.
"""
from platformlib.storeimpl._base import StoreError, _epoch, _json

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
