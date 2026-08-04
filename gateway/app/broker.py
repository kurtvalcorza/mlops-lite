"""Broker plumbing shared by the tenancy, quota, metering, and route modules (026, Phase 0/1).

Two things live here because every broker module needs them and neither belongs to any one of them:
the cached store connection (fail-LOUD — broker work that cannot be recorded must not run, FR-016),
and the **refusal vocabulary**.

## One code, one status (T688)

The refusal codes were the single most-corrected part of this feature's review, because three
different documents gave `gpu_busy` three different statuses (409, 429, 503) and a client's retry
logic cannot act on that. The table is therefore defined once, here, and every route raises through
`refuse()` rather than constructing its own `HTTPException`:

| code | status | permanence |
|---|---|---|
| `unauthorized` | 401 | — missing/invalid/revoked key, or disabled tenant |
| `quota_exhausted` | 403 | until the window resets |
| `model_too_large` | 413 | **permanent** — exceeds `usable_capacity − safety_headroom` |
| `gpu_busy` | 503 + `Retry-After` | **transient**, whatever caused it |
| `metering_unavailable` | 503 | transient |

`413` is reserved for a model that would not fit an **empty** GPU. Contention — another tenant's
outstanding reservation, an exclusive job, a drain that timed out — is always `503 gpu_busy`, because
telling a client to give up on a request that would have succeeded seconds later is worse than making
it retry. The host agent's jobs-lane-full `409` (FR-182) is a different endpoint with a different
meaning and is deliberately not reused for GPU contention.
"""
import threading

from fastapi import HTTPException

from platformlib import store as _store

# -- refusal vocabulary ------------------------------------------------------------------------------

#: code -> (status, transient?). `transient` decides whether `refuse()` attaches `Retry-After`.
REFUSALS = {
    "unauthorized": (401, False),
    "forbidden": (403, False),
    "quota_exhausted": (403, False),
    "not_found": (404, False),
    "model_too_large": (413, False),
    "gpu_busy": (503, True),
    "metering_unavailable": (503, True),
    "store_unavailable": (503, True),
}

#: Default `Retry-After`, in seconds, for a transient refusal that names no better estimate. Short
#: enough that a client retrying on it recovers promptly from ordinary contention; long enough that
#: N refused clients do not rebuild the contention that refused them.
DEFAULT_RETRY_AFTER = 2


def refuse(code: str, message: str = None, *, retry_after: int = None) -> HTTPException:
    """Build the HTTPException for a refusal code. Returned, not raised, so call sites read
    `raise refuse(...)` and a linter can still see the control flow."""
    status, transient = REFUSALS.get(code, (500, False))
    headers = {}
    if transient:
        headers["Retry-After"] = str(retry_after if retry_after is not None else DEFAULT_RETRY_AFTER)
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message or code}},
        headers=headers or None)


# -- store connection (fail-loud, self-healing) ------------------------------------------------------
#
# Mirrors `gateway/app/policies.py`: one cached connection, reopened on the next call after a
# failure, identity-scoped invalidation so a stale invalidator cannot close a healthy reconnection
# another thread just established.

_conn_lock = threading.Lock()
_conn_state = {"conn": None, "bootstrapped": False}


class BrokerStoreError(Exception):
    """The broker store is unreachable. Maps to 503 `store_unavailable` — never a silent no-op:
    admission is refused when a reservation cannot be recorded (FR-016)."""


def conn():
    """The cached broker store connection, opened lazily. Raises BrokerStoreError when unreachable."""
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
            raise BrokerStoreError(f"broker store unreachable: {e}") from e


def invalidate_conn(bad_conn=None) -> None:
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
    """Test seam: CLOSE and drop the cached connection + bootstrap flag.

    Closing matters as much as dropping. A reset that only cleared the reference left the socket
    open until garbage collection, which kept a session on the database — enough to make a
    subsequent `DROP DATABASE` fail with "database is being accessed by other users", and a test
    harness that creates a scratch database per test then leaks one per reset.
    """
    with _conn_lock:
        stale = _conn_state["conn"]
        _conn_state["conn"] = None
        _conn_state["bootstrapped"] = False
    if stale is not None:
        try:
            stale.close()  # outside the lock: closing is I/O, and the lock guards state only
        except Exception:  # noqa: BLE001
            pass
