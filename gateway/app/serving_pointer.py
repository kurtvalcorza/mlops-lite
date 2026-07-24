"""Serving-LLM pointer CRUD (024 US2, T577 — relocated from registry.py; ADR-0005).

Pure Postgres pointer state — `get`/`set`/`restore` — lifted OUT of the MLflow adapter `registry.py`,
where it never belonged: these touch no MLflow, only a gateway-DB connection around the store's
`serving_llm` primitives (with the fail-loud/best-effort normalization the callers rely on).
`registry` re-exports these three names, so `registry.get_serving_llm(...)` / `set_serving_llm(...)` /
`restore_serving_llm(...)` are unchanged for every caller (promotion.go_live, activation, tests) and the
go-live capture/restore stays behavior-identical.

`registry.active_serving_llm_name` deliberately STAYS in the adapter: it resolves cross-authority via
`llmresolve.adopt_active_llm` (MLflow) for the pointer-unset adoption + the configured-default fallback,
so moving it here would either drag MLflow into this relational module or drop the adoption behavior
(changing which LLM serves after an upgrade). `RegistryError` is imported lazily inside the writers so
this module has no import-time dependency on the adapter (no cycle).
"""
import time
from typing import Optional


def _store():
    # Resolve the store through registry's seam (`registry._store`) rather than importing
    # `platformlib.store` directly: that seam is the established store-injection point — the offline
    # suite patches `registry._store` to drive these accessors against a fake (T577 keeps that working
    # with zero test edits). Imported lazily so this module has no load-time dependency on the adapter.
    from . import registry
    return registry._store()


def get_serving_llm() -> Optional[dict]:
    """The active serving-LLM selection {model_name, selected_at, selected_by}, or None when unset
    (or the store is unreachable — both read as 'the configured default base serves')."""
    try:
        s = _store()
        conn = s.connect()
        try:
            return s.get_serving_llm(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort read; unset ⇒ default base
        return None


def set_serving_llm(model_name: str, actor: str = "operator") -> dict:
    """Record `model_name` as the active serving LLM (022 US1 — the gated promote IS the go-live
    action; this is its persistence half, the agent reload is the other). Fail-loud."""
    from .registry import RegistryError
    try:
        s = _store()
        conn = s.connect()
        try:
            # 023 US4: bootstrap() VERIFIES schema compatibility now (gateway startup applies
            # the migrations, FR-299). The guard survives so a promote against an unmigrated or
            # newer-schema DB fails HERE with the migration verdict — before the pointer write
            # half-applies (the original Codex F2 concern, now enforced by the ledger).
            s.bootstrap(conn)
            s.set_serving_llm(conn, model_name, time.time(), actor)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — normalize driver/connection errors
        raise RegistryError(f"active serving-LLM pointer write failed: {e}") from e
    return {"model_name": model_name, "selected_by": actor}


def restore_serving_llm(prior: Optional[dict]) -> None:
    """Roll the active serving-LLM pointer back to a previously-captured value (022 FR-265). Called
    when a promote's reload comes back `unresolvable`: the alias moved but the agent can't load the
    artifact, so the pointer must NOT stay pointed at it — a persisted EXPLICIT pointer to an
    unloadable model 503s the next cold load. `prior` is a `get_serving_llm()` snapshot taken BEFORE
    the promote overwrote it: a concrete model ⇒ restore it; None (was unset) ⇒ clear it, back to
    best-effort adoption / the configured default, which never bricks serving. Fail-loud."""
    from .registry import RegistryError
    try:
        s = _store()
        conn = s.connect()
        try:
            if prior and prior.get("model_name"):
                s.set_serving_llm(conn, prior["model_name"],
                                  prior.get("selected_at") or time.time(),
                                  prior.get("selected_by") or "operator")
            else:
                s.clear_serving_llm(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — normalize driver/connection errors
        raise RegistryError(f"active serving-LLM pointer rollback failed: {e}") from e
