"""ActiveServingLLM pointer repository (022 T461; relocated 024 US1, T571) — the `serving_llm` row.

One platform-scoped row naming the active text-generation model. Written by the gateway's gated
promote (022 US1 — promote = go live); read by the host agent's cold-load resolver. Unset ⇒ the
configured default base serves. Lifted out of `store.py` behind the `store` facade (call sites
unchanged) — it was the last aggregate SQL inline in the facade module. (Per ADR-0005 this is the
pointer CRUD only; `registry.active_serving_llm_name`, which resolves cross-authority via MLflow,
stays a higher-level web-free policy, not in this relational layer.) Timestamps follow the house rule:
epoch floats at the seam, timestamptz in the column.
"""
from platformlib.storeimpl._base import _dt, _epoch


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
