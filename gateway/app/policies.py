"""Per-model policy store + promotion suggestions (018 US3 → US4 T375 — FR-179/182/183).

US4 (T375): the policy state moves off MinIO objects onto the resident Postgres `gateway` DB via
`platformlib.store`. `list_policies`/`list_suggestions` were the O(N) object scans SC-111 kills — they
are now indexed table reads. Per-model layout:

  - `policies` row: the validated ModelPolicy `document`, plus the queue-of-one parked retrain
    (`pending`) and the last check `status` folded onto the same row (both strictly 1:1 per model).
  - `suggestions` rows: promotion suggestions + audit; accept/dismiss is an atomic
    `UPDATE … WHERE state='open'` (the lost-race guard, replacing the read-modify-write).

Failure posture (contract): policy reads AND writes fail LOUD (`PolicyStoreError` → 502) — these are
operator/scheduler operations that must never silently succeed or vanish. `resolve_dataset_version`
still reads dataset manifests from MinIO (dataset artifacts are object-store, not US4 state).

House pattern: pure logic + an injectable `_store`/`_conn()` seam (tests swap a FakeStore in, like
quality.py); a store blip self-heals via identity-scoped `_invalidate_conn`.
"""
import os
import threading
import time
import uuid

from platformlib import store as _store_mod
from platformlib.contracts import ContractError, ModelPolicy, PendingRetrain, PromotionSuggestion

RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")

# The relational store module (US4). Module-level so tests inject an in-memory fake
# (`policies._store = FakeStore()`); production uses platformlib.store against the `gateway` DB.
_store = _store_mod
_conn_state = {"conn": None, "bootstrapped": False}
_conn_lock = threading.Lock()


class PolicyError(Exception):
    """Invalid policy input (maps to 400 with the structured error list)."""


class PolicyStoreError(Exception):
    """The policy store is unreachable / a store op failed (maps to 502)."""


class SuggestionConflict(Exception):
    """The suggestion is already resolved — a lost accept/dismiss race (maps to 409, not 400).
    Deliberately NOT a PolicyError subclass: 400 says "bad request", but the request was fine,
    someone else just got there first (Codex round 4 / claude review, 018)."""


# --- store connection (fail-loud, self-healing) ---------------------------------------------------

def _conn():
    """The cached store connection, opened lazily. Fail-LOUD: raises PolicyStoreError when the store is
    unreachable (policy ops must not silently no-op). Bootstraps the schema once on first success."""
    with _conn_lock:
        c = _conn_state["conn"]
        if c is not None and not getattr(c, "closed", False):
            return c
        try:
            c = _store.connect()
            if not _conn_state["bootstrapped"]:
                _store.bootstrap(c)
                _conn_state["bootstrapped"] = True
            _conn_state["conn"] = c
            return c
        except Exception as e:  # noqa: BLE001 — normalize any driver/connection error
            _conn_state["conn"] = None
            raise PolicyStoreError(f"policy store unreachable: {e}") from e


def _invalidate_conn(bad_conn=None) -> None:
    """Drop+close the cached connection so the next `_conn()` reconnects — identity-scoped so a stale
    invalidator can't close a healthy reconnection another thread established (mirrors quality.py)."""
    with _conn_lock:
        c = _conn_state["conn"]
        if bad_conn is not None and c is not bad_conn:
            return
        _conn_state["conn"] = None
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def reset_conn() -> None:
    """Test seam: drop the cached connection + bootstrap flag (so a test can swap `_store`)."""
    with _conn_lock:
        _conn_state["conn"] = None
        _conn_state["bootstrapped"] = False


def _op(fn):
    """Run a store op on the cached connection, mapping any failure to PolicyStoreError and invalidating
    the connection so a transient blip self-heals on the next call."""
    conn = _conn()
    try:
        return fn(conn)
    except _store.StoreError as e:
        _invalidate_conn(conn)
        raise PolicyStoreError(str(e)) from e
    except Exception as e:  # noqa: BLE001 — any store-op failure is a 502, and drops the connection
        _invalidate_conn(conn)
        raise PolicyStoreError(f"policy store operation failed: {e}") from e


# --- policy CRUD (FR-179) ---------------------------------------------------------------------

def put_policy(model_name: str, doc: dict, updated_by: str = "operator") -> dict:
    doc = dict(doc or {})
    doc["model_name"] = model_name
    doc["updated_at"] = time.time()
    doc["updated_by"] = updated_by
    try:
        policy = ModelPolicy.from_json(doc)
    except ContractError as e:
        raise PolicyError(str(e)) from e  # never store an invalid declaration (FR-179)
    d = policy.to_dict()
    _op(lambda c: _store.put_policy(c, model_name, d, d["updated_at"], updated_by))
    return d


def get_policy(model_name: str):
    doc = _op(lambda c: _store.get_policy(c, model_name))
    return doc if doc and doc.get("model_name") else None


def list_policies() -> list:
    return _op(lambda c: _store.list_policies(c))  # indexed table scan (SC-111: no O(N) object listing)


def delete_policy(model_name: str) -> None:
    _op(lambda c: _store.delete_policy(c, model_name))  # pending + status go with the row


# --- queue-of-one pending retrain (FR-182; durable, restart-resumable) --------------------------

def get_pending(model_name: str):
    return _op(lambda c: _store.get_pending(c, model_name))


def save_pending(pending: dict) -> None:
    PendingRetrain.from_json(pending)  # shape check — never store junk
    _op(lambda c: _store.set_pending(c, pending["model_name"], pending))


def clear_pending(model_name: str) -> None:
    _op(lambda c: _store.clear_pending(c, model_name))


# --- per-model check status (the Monitor page's data source) ------------------------------------

def save_status(model_name: str, status: dict) -> None:
    _op(lambda c: _store.set_status(c, model_name, status))


def get_status(model_name: str):
    return _op(lambda c: _store.get_status(c, model_name))


def policy_status(model_name: str):
    policy = get_policy(model_name)
    if policy is None:
        return None
    return {
        "policy": policy,
        "status": get_status(model_name) or {},
        "pending_retrain": get_pending(model_name),
        "open_suggestions": _op(
            lambda c: _store.list_suggestions(c, state="open", model_name=model_name)),
    }


# --- suggestions + audit (FR-183) ----------------------------------------------------------------

def create_suggestion(model_name: str, candidate_version: str, gate_verdict: dict,
                      shadow_verdict=None, *, state: str = "open", actor=None) -> dict:
    # Idempotent per (model, version, state) — Codex round 4 (018): the scheduler clears its watch only
    # AFTER this write lands, so a status-store blip there re-runs the terminal handling next tick; that
    # retry must not mint a second open suggestion OR a second auto-promoted audit row for the candidate.
    existing = _op(lambda c: _store.find_suggestion(c, model_name, candidate_version, state))
    if existing is not None:
        return existing
    rec = PromotionSuggestion(
        id=uuid.uuid4().hex[:12], model_name=model_name, candidate_version=candidate_version,
        gate_verdict=gate_verdict or {}, shadow_verdict=shadow_verdict, state=state,
        created_at=time.time(),
        resolved_at=time.time() if state == "auto-promoted" else None, actor=actor)
    rec.validate()
    d = rec.to_dict()
    _op(lambda c: _store.create_suggestion(c, d))
    return d


def get_suggestion(suggestion_id: str):
    return _op(lambda c: _store.get_suggestion(c, suggestion_id))


def list_suggestions(state: str = None) -> list:
    return _op(lambda c: _store.list_suggestions(c, state=state))


def resolve_suggestion(suggestion_id: str, state: str, actor: str = "operator"):
    # Atomic terminal transition: the store's `UPDATE … WHERE state='open'` admits exactly one resolver.
    updated = _op(lambda c: _store.resolve_suggestion(c, suggestion_id, state, actor, time.time()))
    if updated is not None:
        return updated
    # 0 rows updated → either the id doesn't exist (→ None/404) or it was already resolved (→ 409). One
    # follow-up read disambiguates (the atomic UPDATE already closed the race window).
    current = get_suggestion(suggestion_id)
    if current is None:
        return None
    raise SuggestionConflict(f"suggestion {suggestion_id} is already {current.get('state')}")


# --- dataset resolution (FR-181) -----------------------------------------------------------------

def _missing(e) -> bool:
    """True only for a confirmed 404-shaped error (same discrimination quality.py uses)."""
    resp = getattr(e, "response", None)
    return isinstance(resp, dict) and resp.get("Error", {}).get("Code") in ("404", "NoSuchKey")


def resolve_dataset_version(name: str, version: str) -> str:
    """`latest` → the newest registered version of `name` (by manifest registered_at); anything
    else is returned as-is (a pinned version). Raises PolicyError when nothing is registered —
    a breach with no data to retrain on is a loud condition, not a silent no-op. Dataset artifacts
    stay in object storage (not US4 state), so this path is unchanged from the pre-cutover code."""
    if version != "latest":
        return version
    from . import datasets

    try:
        rows = [d for d in datasets.list_datasets() if d.get("name") == name]
    except Exception as e:
        raise PolicyStoreError(f"cannot resolve latest version of {name!r}: {e}") from e
    versions = rows[0].get("versions", []) if rows else []
    best, best_at = None, -1.0
    for v in versions:
        m = _manifest(name, v.get("version"))
        at = (m or {}).get("registered_at") or 0
        if at > best_at:
            best, best_at = v.get("version"), at
    if not best:
        raise PolicyError(f"dataset {name!r} has no registered versions to retrain on")
    return best


def _manifest(name: str, version: str):
    """None only for a confirmed-missing manifest; a transient read failure RAISES (Codex
    round 2, 018) — treating it as missing could silently resolve `latest` to an OLDER version
    and retrain on stale data while reporting success."""
    import json

    from . import datasets

    try:
        raw = datasets._s3().get_object(Bucket=datasets.BUCKET,
                                        Key=f"{name}/{version}/manifest.json")["Body"].read()
        return json.loads(raw)
    except Exception as e:
        if _missing(e):
            return None
        raise PolicyStoreError(
            f"cannot read manifest {name}/{version} while resolving 'latest': {e}") from e
