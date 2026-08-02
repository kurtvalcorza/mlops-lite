"""Per-request GPU-seconds metering: reserve before work, settle to actual after (026 T627–T631).

The store layer (`platformlib.storeimpl.metering`) owns the atomicity and the window binding. This
module owns the two things that are the *gateway's* problem:

## The fail-safe direction (FR-016)

A GPU operation runs only if its **reservation** was recorded. Reserve failure refuses the work —
that is the whole point of reserving rather than logging afterwards, and it is why `reserve_or_refuse`
raises rather than returning a sentinel a caller could forget to check.

**Settlement is the opposite direction.** By the time a settle happens the GPU-seconds have already
been spent; refusing to record them does not un-spend them, it only loses the record. So a settle
that cannot reach the store is written to a local durable outbox and retried, never dropped and
never surfaced to the tenant as a failure of their request.

## The outbox (T629)

A JSONL write-ahead log, fsynced before the store write is attempted, replayed at startup.
`store.settle` is idempotent — the ledger row is uniquely keyed by `ref_id` and the state transition
is guarded on `state = 'reserved'` — so replaying an entry that already committed changes nothing.
That idempotence is what makes "killed mid-settle" settle **exactly once** on restart: the WAL
guarantees at-least-once delivery and the store constraint collapses it to exactly-once. Neither
half is sufficient alone, which is why both exist.
"""
import json
import logging
import os
import threading
import time

from platformlib import store as _store
from platformlib.storeimpl.metering import QuotaExhausted

from .broker import BrokerStoreError, conn, invalidate_conn, refuse

logger = logging.getLogger("gateway.metering")

#: Where the settlement outbox lives. A path the gateway can fsync — inside the container's writable
#: state directory, not /tmp, because a restart must find it.
DEFAULT_WAL_PATH = os.getenv("BROKER_METERING_WAL", "/var/lib/mlops/broker-settlements.jsonl")

#: Default pre-authorization for one inference request, in GPU-seconds. Deliberately generous
#: relative to a typical completion: an under-estimate would let a tenant overshoot its budget by
#: the difference on every request, while an over-estimate only makes the tenant's own quota briefly
#: more conservative and is released the moment the request settles.
DEFAULT_INFERENCE_ESTIMATE_S = float(os.getenv("BROKER_INFERENCE_ESTIMATE_S", "30"))

_wal_lock = threading.Lock()


def wal_path() -> str:
    return os.getenv("BROKER_METERING_WAL", DEFAULT_WAL_PATH)


def _wal_append(record: dict) -> None:
    """Append one durable line. fsync before returning — an entry the caller believes is durable but
    that sits in the page cache is exactly the entry a power loss eats."""
    path = wal_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with _wal_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def _wal_entries() -> list:
    path = wal_path()
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                logger.warning("skipping unparseable settlement WAL line")
    return entries


def _wal_compact(done_op_ids: set) -> None:
    """Drop entries whose settlement is confirmed. Rewrites through a temp file + `os.replace` so a
    crash mid-compaction leaves the old complete WAL rather than a truncated one.

    **The read and the rewrite are one critical section.** They used not to be: the entries were
    read *before* taking the lock, so an append landing between the read and the `os.replace` was
    erased by the stale snapshot. That is silent data loss in the one structure whose entire job is
    to survive loss — and it loses precisely the record needed if that request's database settlement
    later fails, breaking the exactly-once guarantee the outbox exists to provide.

    `_wal_entries()` does not take the lock itself, so reading inside it here is safe with a
    non-reentrant lock.
    """
    path = wal_path()
    tmp = path + ".compact"
    with _wal_lock:
        remaining = [e for e in _wal_entries()
                     if not (e.get("op_id") in done_op_ids or e.get("done"))]
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in remaining:
                fh.write(json.dumps(e, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


# -- reserve --------------------------------------------------------------------------------------

def _existing_settled(c, op_id: str) -> bool:
    """Whether a reservation under this op id has already settled.

    Best-effort: an unreadable store here must not fail the request, because the reserve below has
    its own fail-closed handling and would refuse for a better-stated reason.
    """
    try:
        existing = _store.get_reservation(c, op_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(existing) and existing.get("state") == "settled"


def reserve_or_refuse(op_id: str, tenant: dict, est_gpu_seconds: float = None, *,
                      kind: str = "inference", modality: str = "") -> dict:
    """Pre-authorize GPU-seconds, or raise the HTTP refusal that matches the reason.

    `403 quota_exhausted` when the window budget cannot cover the estimate; `503
    metering_unavailable` when the reservation cannot be recorded at all. The second is the FR-016
    fail-safe: work whose consumption cannot be recorded must not run, because it would be GPU time
    charged to nobody.
    """
    est = DEFAULT_INFERENCE_ESTIMATE_S if est_gpu_seconds is None else est_gpu_seconds
    try:
        c = conn()
    except BrokerStoreError as e:
        raise refuse("metering_unavailable", f"usage cannot be recorded: {e}")

    # Idempotency has to cover EXECUTION, not just accounting.
    #
    # `store.reserve()` returns an existing reservation for a repeated op id, which is right for a
    # client retrying a request it is unsure landed. But a reservation that has already **settled**
    # is finished, and returning it let the caller run GPU work again — while `settle()`, idempotent
    # by design, recorded nothing the second time. A tenant replaying one `X-Request-Id` therefore
    # got unlimited free inference, and the retry test could not see it: it asserted one ledger row,
    # which is exactly what a free re-run produces.
    #
    # Refused rather than silently re-reserved: a client reusing a settled id has a bug, and minting
    # a fresh charge under an id it believes is idempotent would surprise it in the other direction.
    settled = _existing_settled(c, op_id)
    if settled:
        raise refuse("forbidden",
                     f"request id {op_id.split(':', 1)[-1]!r} has already been settled — reusing it "
                     "would run new GPU work under a finished reservation. Send a new X-Request-Id.")

    try:
        return _store.reserve(c, op_id, tenant["id"], est, kind=kind, modality=modality)
    except QuotaExhausted as e:
        raise refuse("quota_exhausted", str(e))
    except Exception as e:  # noqa: BLE001 — one retry through a fresh connection, then refuse
        invalidate_conn(c)
        try:
            return _store.reserve(conn(), op_id, tenant["id"], est, kind=kind, modality=modality)
        except QuotaExhausted as e2:
            raise refuse("quota_exhausted", str(e2))
        except Exception:  # noqa: BLE001
            raise refuse("metering_unavailable", f"usage cannot be recorded: {e}")


# -- settle ---------------------------------------------------------------------------------------

def settle(op_id: str, actual_gpu_seconds: float) -> dict:
    """Settle to actual, durably. Never raises to the caller — see the module docstring for why
    settlement fails forward while reservation fails closed.

    Returns `{"settled": bool, "deferred": bool, "gpu_seconds": float}` so a route can decide what
    to report (`X-GPU-Seconds` on a settled charge; nothing extra on a deferred one, which will
    reconcile out of band).
    """
    entry = {"op_id": op_id, "gpu_seconds": float(actual_gpu_seconds), "at": time.time()}
    _wal_append(entry)
    try:
        c = conn()
        _store.settle(c, op_id, actual_gpu_seconds)
        _wal_compact({op_id})
        return {"settled": True, "deferred": False, "gpu_seconds": float(actual_gpu_seconds)}
    except Exception as e:  # noqa: BLE001 — the seconds are already spent; the record can wait
        invalidate_conn()
        logger.warning("settlement for %s deferred to the outbox: %s", op_id, e)
        return {"settled": False, "deferred": True, "gpu_seconds": float(actual_gpu_seconds)}


def release(op_id: str) -> None:
    """Release a reservation in full (the work never ran). Best-effort with a WAL fallback, for the
    same reason as `settle`: an un-released reservation over-charges the tenant until it is
    reconciled, which is a wrong number, not a lost one."""
    _wal_append({"op_id": op_id, "release": True, "at": time.time()})
    try:
        _store.release(conn(), op_id)
        _wal_compact({op_id})
    except Exception as e:  # noqa: BLE001
        invalidate_conn()
        logger.warning("release for %s deferred to the outbox: %s", op_id, e)


def reservation_ttl_s() -> float:
    """How long an inference reservation may sit `reserved` before the sweep reclaims it.

    Must stay comfortably above the longest plausible request plus the longest plausible
    deferred-settlement delay: a genuine settle arriving after the sweep released its reservation
    is dropped (by design — see `storeimpl.metering.settle`), so a too-short TTL trades a stuck
    quota for lost charges. An hour is ~120× the default per-request estimate.
    """
    return float(os.getenv("BROKER_INFERENCE_RESERVATION_TTL_S", "3600"))


def sweep_orphaned_reservations() -> dict:
    """Release inference reservations stuck `reserved` past the TTL (startup + periodic).

    The leak this reaps: a client that disconnects before Starlette ever pulls the response body
    leaves the stream generator unstarted, so neither the generator's `finally` nor `_Meter` runs
    a settle or release. Best-effort like every settlement-direction write — a failed sweep only
    means the quota stays over-reserved until the next tick.
    """
    try:
        swept = _store.sweep_stale_reservations(conn(), older_than_s=reservation_ttl_s())
    except Exception as e:  # noqa: BLE001 — store blip: the next tick retries
        invalidate_conn()
        logger.warning("orphaned-reservation sweep failed, will retry: %s", e)
        return {"swept": 0, "failed": True}
    if swept:
        logger.warning("released %d orphaned inference reservation(s) older than %.0fs: %s",
                       len(swept), reservation_ttl_s(), ", ".join(swept[:10]))
    return {"swept": len(swept), "failed": False}


def replay_outbox() -> dict:
    """Replay every unsettled WAL entry (gateway startup, T629).

    Safe to run at any time: each store operation is idempotent, so an entry whose settlement already
    committed is a no-op rather than a double charge.
    """
    entries = _wal_entries()
    if not entries:
        return {"replayed": 0, "failed": 0}
    replayed, failed, done = 0, 0, set()
    for e in entries:
        op_id = e.get("op_id")
        if not op_id:
            continue
        try:
            c = conn()
            if e.get("release"):
                _store.release(c, op_id)
            else:
                _store.settle(c, op_id, float(e.get("gpu_seconds") or 0.0))
            done.add(op_id)
            replayed += 1
        except Exception as ex:  # noqa: BLE001 — leave it in the WAL for the next attempt
            invalidate_conn()
            logger.warning("outbox replay for %s failed, will retry: %s", op_id, ex)
            failed += 1
    if done:
        _wal_compact(done)
    logger.info("settlement outbox replay: %d settled, %d still pending", replayed, failed)
    return {"replayed": replayed, "failed": failed}


# -- quota reads ------------------------------------------------------------------------------------

def quota_state(tenant_id: str) -> dict:
    """The tenant's current-window position, or an empty shape when the store is unreachable.

    Read-only and advisory — used for `X-Quota-Remaining` and the admin usage view. A failure here
    must not fail the request it decorates, so it degrades to nulls rather than raising.
    """
    try:
        return _store.consumption(conn(), tenant_id)
    except Exception:  # noqa: BLE001
        invalidate_conn()
        return {"window_start": None, "budget_gpu_seconds": None, "consumed_gpu_seconds": None,
                "remaining_gpu_seconds": None}
