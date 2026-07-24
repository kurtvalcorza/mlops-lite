"""Promotion-suggestions repository (024 US1, T570) — the `suggestions` audit rows (FR-183).

Lifted from `store.py` behind the `store` facade (call sites unchanged). Indexed table reads replace
the pre-US4 per-object `suggestions/*.json` listing; created_at/resolved_at restore to epoch floats at
the seam. `resolve_suggestion`'s `state = 'open'` guard makes accept/dismiss atomic in the DB.
"""
from platformlib.storeimpl._base import _dt, _epoch, _json

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
