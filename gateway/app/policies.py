"""Per-model policy store + promotion suggestions (018 US3, T367/T370 — FR-179/182/183).

Storage rides MinIO pre-US4 (spec Assumptions): `policies/{model}.json`, the queue-of-one parked
retrain at `policies/_pending/{model}.json`, per-model check status at
`policies/_status/{model}.json`, and suggestions/audit at `suggestions/{id}.json` — all in the
existing results bucket; everything moves to the relational store at T375. Validation is the
shared contract (`platformlib.contracts.ModelPolicy`) so an invalid declaration is rejected with
a structured error and never stored (FR-179).

House pattern: pure logic + an injectable `_s3()` seam (tests swap a fake in, like quality.py).
"""
import os
import time
import uuid

from platformlib import store
from platformlib.contracts import ContractError, ModelPolicy, PendingRetrain, PromotionSuggestion

RESULTS_BUCKET = os.getenv("RESULTS_BUCKET", "results")
POLICY_PREFIX = "policies/"
PENDING_PREFIX = "policies/_pending/"
STATUS_PREFIX = "policies/_status/"
SUGGESTION_PREFIX = "suggestions/"


class PolicyError(Exception):
    """Invalid policy input (maps to 400 with the structured error list)."""


class PolicyStoreError(Exception):
    """The policy store is unreachable (maps to 502)."""


class SuggestionConflict(Exception):
    """The suggestion is already resolved — a lost accept/dismiss race (maps to 409, not 400).
    Deliberately NOT a PolicyError subclass: 400 says "bad request", but the request was fine,
    someone else just got there first (Codex round 4 / claude review, 018)."""


def _s3():
    return store.s3_client()


def _put(key: str, obj: dict) -> None:
    import json

    try:
        _s3().put_object(Bucket=RESULTS_BUCKET, Key=key, Body=json.dumps(obj).encode(),
                         ContentType="application/json")
    except Exception as e:
        raise PolicyStoreError(f"cannot write {key}: {e}") from e


def _missing(e) -> bool:
    """True only for a confirmed 404-shaped error (same discrimination quality.py uses)."""
    resp = getattr(e, "response", None)
    return isinstance(resp, dict) and resp.get("Error", {}).get("Code") in ("404", "NoSuchKey")


def _get(key: str):
    """None ONLY for a confirmed-missing key. A transient S3/MinIO failure must raise (Codex
    review, 018): mapping it to None turned store outages into false 404s and could make the
    scheduler believe a parked PendingRetrain vanished — losing the retry."""
    import json

    try:
        return json.loads(_s3().get_object(Bucket=RESULTS_BUCKET, Key=key)["Body"].read())
    except Exception as e:
        if _missing(e):
            return None
        raise PolicyStoreError(f"cannot read {key}: {e}") from e


def _delete(key: str) -> None:
    """Idempotent on a missing key; a transient failure RAISES (Codex round 2, 018) — a
    swallowed delete let DELETE /policies return success while the policy (and its pending
    retrain) survived the outage and kept scheduling."""
    try:
        _s3().delete_object(Bucket=RESULTS_BUCKET, Key=key)
    except Exception as e:
        if not _missing(e):
            raise PolicyStoreError(f"cannot delete {key}: {e}") from e


# --- policy CRUD (FR-179) ---------------------------------------------------------------------

def put_policy(model_name: str, doc: dict, updated_by: str = "operator") -> dict:
    doc = dict(doc or {})
    doc["model_name"] = model_name
    doc["updated_at"] = time.time()
    doc["updated_by"] = updated_by
    try:
        policy = ModelPolicy.from_json(doc)
    except ContractError as e:
        raise PolicyError(str(e)) from e
    _put(f"{POLICY_PREFIX}{model_name}.json", policy.to_dict())
    return policy.to_dict()


def get_policy(model_name: str):
    doc = _get(f"{POLICY_PREFIX}{model_name}.json")
    return doc if doc and doc.get("model_name") else None


def list_policies() -> list:
    try:
        keys = store.list_keys(_s3(), RESULTS_BUCKET, POLICY_PREFIX)
    except Exception as e:
        raise PolicyStoreError(f"cannot list policies: {e}") from e
    out = []
    for k in keys:
        if k.startswith((PENDING_PREFIX, STATUS_PREFIX)):
            continue  # sub-prefixes share the policies/ root
        doc = _get(k)
        if doc and doc.get("model_name"):
            out.append(doc)
    out.sort(key=lambda d: d.get("model_name", ""))
    return out


def delete_policy(model_name: str) -> None:
    _delete(f"{POLICY_PREFIX}{model_name}.json")
    _delete(f"{PENDING_PREFIX}{model_name}.json")
    _delete(f"{STATUS_PREFIX}{model_name}.json")


# --- queue-of-one pending retrain (FR-182; durable, restart-resumable) --------------------------

def get_pending(model_name: str):
    return _get(f"{PENDING_PREFIX}{model_name}.json")


def save_pending(pending: dict) -> None:
    PendingRetrain.from_json(pending)  # shape check — never store junk
    _put(f"{PENDING_PREFIX}{pending['model_name']}.json", pending)


def clear_pending(model_name: str) -> None:
    _delete(f"{PENDING_PREFIX}{model_name}.json")


# --- per-model check status (the Monitor page's data source) ------------------------------------

def save_status(model_name: str, status: dict) -> None:
    _put(f"{STATUS_PREFIX}{model_name}.json", status)


def get_status(model_name: str):
    return _get(f"{STATUS_PREFIX}{model_name}.json")


def policy_status(model_name: str):
    policy = get_policy(model_name)
    if policy is None:
        return None
    return {
        "policy": policy,
        "status": get_status(model_name) or {},
        "pending_retrain": get_pending(model_name),
        "open_suggestions": [s for s in list_suggestions() if
                             s.get("model_name") == model_name and s.get("state") == "open"],
    }


# --- suggestions + audit (FR-183) ----------------------------------------------------------------

def create_suggestion(model_name: str, candidate_version: str, gate_verdict: dict,
                      shadow_verdict=None, *, state: str = "open", actor=None) -> dict:
    # Idempotent per (model, version, state) — Codex round 4 (018): the scheduler clears its
    # watch only AFTER this write lands, so a status-store blip there re-runs the terminal
    # handling next tick; that retry must not mint a second open suggestion (or a second
    # auto-promoted audit row) for the same candidate.
    try:
        for s in list_suggestions(state=state):
            if (s.get("model_name") == model_name
                    and str(s.get("candidate_version")) == str(candidate_version)):
                return s
    except PolicyStoreError:
        pass  # listing down — fall through to the write (worst case is the old duplicate risk)
    rec = PromotionSuggestion(
        id=uuid.uuid4().hex[:12], model_name=model_name, candidate_version=candidate_version,
        gate_verdict=gate_verdict or {}, shadow_verdict=shadow_verdict, state=state,
        created_at=time.time(),
        resolved_at=time.time() if state == "auto-promoted" else None, actor=actor)
    rec.validate()
    _put(f"{SUGGESTION_PREFIX}{rec.id}.json", rec.to_dict())
    return rec.to_dict()


def get_suggestion(suggestion_id: str):
    return _get(f"{SUGGESTION_PREFIX}{suggestion_id}.json")


def list_suggestions(state: str = None) -> list:
    try:
        keys = store.list_keys(_s3(), RESULTS_BUCKET, SUGGESTION_PREFIX)
    except Exception as e:
        raise PolicyStoreError(f"cannot list suggestions: {e}") from e
    out = [s for s in (_get(k) for k in keys) if s]
    if state:
        out = [s for s in out if s.get("state") == state]
    out.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return out


def resolve_suggestion(suggestion_id: str, state: str, actor: str = "operator"):
    rec = get_suggestion(suggestion_id)
    if rec is None:
        return None
    if rec.get("state") != "open":
        raise SuggestionConflict(f"suggestion {suggestion_id} is already {rec.get('state')}")
    rec.update(state=state, resolved_at=time.time(), actor=actor)
    _put(f"{SUGGESTION_PREFIX}{suggestion_id}.json", rec)
    return rec


# --- dataset resolution (FR-181) -----------------------------------------------------------------

def resolve_dataset_version(name: str, version: str) -> str:
    """`latest` → the newest registered version of `name` (by manifest registered_at); anything
    else is returned as-is (a pinned version). Raises PolicyError when nothing is registered —
    a breach with no data to retrain on is a loud condition, not a silent no-op."""
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
