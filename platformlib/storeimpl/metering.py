"""Metering repository — reserve-then-settle GPU-seconds accounting (026 T628–T631, T678).

GPU-seconds is the canonical unit; "credits" is only a display alias. The shape of this module is
dictated by one fact the original design got wrong: **final GPU-seconds are not knowable up front**,
so FR-016's "record before work" cannot mean "write the ledger row first". It means:

    reserve (atomic, refuses on overflow)  ->  do the work  ->  settle actual + release the remainder

Three properties are enforced here, each because enforcing them at the call site failed at least
once in review:

  * **Reserve is atomic against concurrent reserves.** Summing the outstanding reservations and
    inserting a new one in two statements is textbook write-skew: two requests against a budget with
    room for one both read the same sum and both insert. The reserve path therefore takes a row lock
    on the tenant's `quotas` row first, which serializes reservations *per tenant* without a global
    bottleneck. See `reserve` for why the quota row is the right lock and not an advisory key.

  * **Reserve is idempotent, keyed by `op_id`.** A retried request must not double-charge. The
    primary key does the work; a second reserve for the same `op_id` returns the existing
    reservation unchanged rather than failing, because the caller's retry is not an error.

  * **Every charge is bound to the window stamped at reserve time** (T678). A job reserved just
    before a boundary and finishing after it charges the OLD window. Deriving the window at
    completion instead lets a tenant reserve the new window's full budget while an old job is still
    outstanding, overshooting the budget that authorized it — and consumption is therefore derived
    from rows *bearing* a `window_start`, never from a `ts` range.
"""
from platformlib.storeimpl._base import StoreError
from platformlib.storeimpl.tenancy import get_quota, window_start_sql

#: Returned by `reserve` when the tenant's window budget cannot cover the estimate.
QUOTA_EXHAUSTED = "quota_exhausted"


class QuotaExhausted(StoreError):
    """The tenant's window budget is spent (maps to 403 `quota_exhausted`, FR-014)."""

    def __init__(self, tenant_id: str, remaining: float, requested: float):
        self.tenant_id = tenant_id
        self.remaining = remaining
        self.requested = requested
        super().__init__(
            f"quota exhausted: {requested:.3f} GPU-seconds requested, {remaining:.3f} remaining")


def _row_to_reservation(row) -> dict:
    (op_id, tenant_id, kind, modality, window_start, est, settled, state) = row
    return {"op_id": op_id, "tenant_id": tenant_id, "kind": kind, "modality": modality,
            "window_start": window_start, "est_gpu_seconds": float(est),
            "settled_gpu_seconds": float(settled) if settled is not None else None,
            "state": state}


_RESERVATION_SELECT = (
    "SELECT op_id, tenant_id, kind, modality, window_start, est_gpu_seconds, "
    "settled_gpu_seconds, state FROM usage_reservation")


def get_reservation(conn, op_id: str):
    with conn.cursor() as cur:
        cur.execute(_RESERVATION_SELECT + " WHERE op_id = %s", (op_id,))
        row = cur.fetchone()
    return _row_to_reservation(row) if row else None


# -- consumption ------------------------------------------------------------------------------------

def consumption(conn, tenant_id: str, window: str = None, window_start=None) -> dict:
    """A tenant's current-window position: `{window_start, budget, settled, outstanding, consumed,
    remaining}`, all GPU-seconds.

    `consumed` counts BOTH settled ledger rows and still-outstanding reservations. Counting only
    the ledger would let a tenant with several long jobs in flight reserve far past its budget,
    since none of them has settled yet — the outstanding estimate is precisely the part of the
    budget already promised away.
    """
    quota = get_quota(conn, tenant_id)
    if quota is None and window is None and window_start is None:
        return {"window_start": None, "budget_gpu_seconds": None, "settled_gpu_seconds": 0.0,
                "outstanding_gpu_seconds": 0.0, "consumed_gpu_seconds": 0.0,
                "remaining_gpu_seconds": None, "window": None}
    window = window or (quota or {}).get("window") or "daily"
    with conn.cursor() as cur:
        if window_start is None:
            cur.execute(f"SELECT {window_start_sql(window)}")
            window_start = cur.fetchone()[0]
        cur.execute(
            "SELECT coalesce((SELECT sum(gpu_seconds) FROM usage_ledger "
            "                 WHERE tenant_id = %s AND window_start = %s), 0), "
            "       coalesce((SELECT sum(est_gpu_seconds) FROM usage_reservation "
            "                 WHERE tenant_id = %s AND window_start = %s AND state = 'reserved'), 0)",
            (tenant_id, window_start, tenant_id, window_start))
        settled, outstanding = cur.fetchone()
    settled, outstanding = float(settled), float(outstanding)
    budget = float(quota["budget_gpu_seconds"]) if quota else None
    consumed = settled + outstanding
    return {"window_start": window_start, "window": window, "budget_gpu_seconds": budget,
            "settled_gpu_seconds": settled, "outstanding_gpu_seconds": outstanding,
            "consumed_gpu_seconds": consumed,
            "remaining_gpu_seconds": None if budget is None else budget - consumed}


# -- reserve ----------------------------------------------------------------------------------------

class ReservationFinished(StoreError):
    """The op id has already been settled or released — it cannot authorize new execution.

    Distinct from `QuotaExhausted` because the caller's response differs: quota exhaustion is
    retryable (with a different id or after the window rolls), while a finished reservation means
    the client reused an id it should not have.
    """

    def __init__(self, op_id: str, state: str):
        self.op_id = op_id
        self.state = state
        super().__init__(
            f"reservation {op_id!r} is already {state!r} — reusing it would run new GPU work "
            "under a finished reservation")


def reserve(conn, op_id: str, tenant_id: str, est_gpu_seconds: float, *, kind: str = "inference",
            modality: str = "", default_budget_gpu_seconds: float = None) -> dict:
    """Atomically pre-authorize `est_gpu_seconds` against the tenant's current window.

    Returns the reservation dict. Raises `QuotaExhausted` when the window budget cannot cover it,
    `ReservationFinished` when the op id has already settled or been released, and `StoreError`
    when the reservation cannot be recorded at all — the caller refuses the GPU work in all three
    cases (FR-016 fail-safe: work that cannot be metered does not run).

    **Why the quota row is the lock.** Serialization has to happen somewhere, and the choices were a
    per-tenant advisory lock or the quota row itself. The row wins: it already exists exactly once
    per tenant, it is the thing being checked, and `SELECT … FOR UPDATE` releases it with the
    transaction — so a caller that dies mid-reserve cannot leak the lock, which an advisory key
    held on a pooled connection can. Contention is per tenant, so one tenant's burst never
    serializes another's.

    A tenant with **no quota row** is unmetered-but-recorded: the reservation is still written (so
    usage is attributable and settles normally) and no budget is enforced. That is the permissive
    direction, chosen because `POST /admin/tenants` hands out a working key before any quota is set;
    pass `default_budget_gpu_seconds` to make the absent case bounded instead.
    """
    if est_gpu_seconds < 0:
        raise StoreError("est_gpu_seconds must be >= 0")

    existing = get_reservation(conn, op_id)
    if existing is not None:
        # ANY pre-existing reservation — regardless of state — blocks a second execution.
        #
        # `reserved`: another request is currently executing GPU work under this op id (or the
        # original request crashed and the orphan sweep has not reclaimed it yet). Either way, a
        # second execution under the same op id would produce two GPU calls billed as one: the
        # ledger is keyed by op_id, so only one settlement survives.
        #
        # `settled`/`released`: the op is finished; replaying it would run free GPU work because
        # `settle()` is idempotent and records nothing the second time.
        #
        # The fail-closed direction: refuse. A client whose response was lost should generate a
        # new X-Request-Id for the retry. The orphan sweep reclaims genuinely abandoned
        # reservations after the TTL, restoring the tenant's quota.
        state = "in_flight" if existing["state"] == "reserved" else existing["state"]
        raise ReservationFinished(op_id, state)

    with conn.transaction():
        with conn.cursor() as cur:
            # Lock the tenant's quota row (if any) for the rest of the transaction. NOWAIT is
            # deliberately NOT used: a concurrent reserve for the same tenant is expected and should
            # queue behind this one, not fail.
            cur.execute("SELECT quota_window, budget_gpu_seconds FROM quotas WHERE tenant_id = %s "
                        "FOR UPDATE", (tenant_id,))
            qrow = cur.fetchone()
            if qrow is None:
                window, budget = "daily", default_budget_gpu_seconds
            else:
                window, budget = qrow[0], float(qrow[1])

            cur.execute(f"SELECT {window_start_sql(window)}")
            window_start = cur.fetchone()[0]

            if budget is not None:
                cur.execute(
                    "SELECT coalesce((SELECT sum(gpu_seconds) FROM usage_ledger "
                    "                 WHERE tenant_id = %s AND window_start = %s), 0) "
                    "     + coalesce((SELECT sum(est_gpu_seconds) FROM usage_reservation "
                    "                 WHERE tenant_id = %s AND window_start = %s "
                    "                   AND state = 'reserved'), 0)",
                    (tenant_id, window_start, tenant_id, window_start))
                consumed = float(cur.fetchone()[0])
                remaining = budget - consumed
                if est_gpu_seconds > remaining:
                    raise QuotaExhausted(tenant_id, remaining, est_gpu_seconds)

            cur.execute(
                "INSERT INTO usage_reservation (op_id, tenant_id, kind, modality, window_start, "
                "est_gpu_seconds, state) VALUES (%s, %s, %s, %s, %s, %s, 'reserved') "
                "ON CONFLICT (op_id) DO NOTHING",
                (op_id, tenant_id, kind, modality, window_start, est_gpu_seconds))
            inserted = cur.rowcount == 1

    reservation = get_reservation(conn, op_id)
    if reservation is None:  # pragma: no cover — the insert committed or the transaction raised
        raise StoreError(f"reservation {op_id!r} could not be recorded")

    if not inserted:
        # Another concurrent request with the same op_id won the insert race. The quota-row lock
        # serialized the transactions, so the winner committed first and this request's INSERT was
        # a no-op. We now see the winner's row.
        #
        # If it's still `reserved`, the winner is currently executing GPU work — admitting a second
        # execution under the same reservation would run two GPU calls billed as one.
        # If it settled or was released in the meantime, the id is finished.
        if reservation["state"] == "reserved":
            raise ReservationFinished(op_id, "in_flight")
        raise ReservationFinished(op_id, reservation["state"])

    return reservation


# -- settle / release -------------------------------------------------------------------------------

def settle(conn, op_id: str, actual_gpu_seconds: float) -> dict:
    """Settle a reservation to its actual consumption and release the remainder (T629).

    **Idempotent by construction**, which is what makes the WAL replay in `gateway/app/metering.py`
    safe: the ledger row is keyed uniquely by `ref_id` and the state transition is guarded by
    `state = 'reserved'`, so re-running a settle that already committed changes nothing. A process
    killed between the ledger insert and the state update therefore settles exactly once on restart
    rather than twice or not at all.

    The ledger row carries the reservation's **stored** `window_start`, not the current window
    (T678) — the charge belongs to the window that authorized it.
    """
    if actual_gpu_seconds < 0:
        raise StoreError("actual_gpu_seconds must be >= 0")
    reservation = get_reservation(conn, op_id)
    if reservation is None:
        raise StoreError(f"no reservation {op_id!r} to settle")
    if reservation["state"] != "reserved":
        # Already settled (idempotent replay) — or released, by an explicit refund or the stale
        # sweep. Inserting the ledger row anyway would charge against a reservation whose
        # `settled_gpu_seconds` is pinned at 0/its old value, breaking `reconciliation()`'s
        # identity permanently with no correction path (the ledger is append-only and uniquely
        # keyed). The kill-mid-settle replay is unaffected: that crash leaves the row `reserved`.
        return reservation

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage_ledger (tenant_id, kind, ref_id, modality, gpu_seconds, "
                "window_start) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (ref_id) DO NOTHING",
                (reservation["tenant_id"], reservation["kind"], op_id, reservation["modality"],
                 actual_gpu_seconds, reservation["window_start"]))
            cur.execute(
                "UPDATE usage_reservation SET state = 'settled', settled_at = now(), "
                "settled_gpu_seconds = %s WHERE op_id = %s AND state = 'reserved'",
                (actual_gpu_seconds, op_id))
    return get_reservation(conn, op_id)


def release(conn, op_id: str) -> dict:
    """Release a reservation in full without charging (cancelled before any GPU work).

    Idempotent for the same reason `settle` is: the transition is guarded on `state = 'reserved'`,
    so a repeated release is a no-op rather than a second refund.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE usage_reservation SET state = 'released', settled_at = now(), "
                    "settled_gpu_seconds = 0 WHERE op_id = %s AND state = 'reserved'", (op_id,))
    return get_reservation(conn, op_id)


def sweep_stale_reservations(conn, *, older_than_s: float, kind: str = "inference") -> list:
    """Release reservations stuck in `reserved` past any plausible lifetime. Returns their op ids.

    Closes the one leak the reserve→settle contract cannot close from inside a request: a client
    that disconnects before the response body is ever pulled leaves the stream generator unstarted,
    so no `finally` runs and nothing settles or releases (there is no suspended frame for
    `GeneratorExit` to reach). The reservation then holds its estimate against the tenant's window
    forever — quota permanently consumed by work that never happened.

    Scoped to one `kind` because inference is the only kind with a bounded lifetime: a `job` or
    `session` reservation legitimately stays `reserved` for as long as the work runs, and sweeping
    those would refund quota for GPU time still being spent — then void the real settle when it
    arrived.

    Releases rather than settles: work that actually ran settles through the WAL replay well inside
    any sane threshold, so what this reaps is work that never started. The threshold must therefore
    stay comfortably above the longest plausible deferred-settlement delay — a settle arriving
    *after* the sweep is a no-op (see `settle`), which loses that charge rather than corrupting the
    reconciliation identity.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usage_reservation SET state = 'released', settled_at = now(), "
            "settled_gpu_seconds = 0 WHERE kind = %s AND state = 'reserved' "
            "AND created_at < now() - make_interval(secs => %s) RETURNING op_id",
            (kind, float(older_than_s)))
        return [row[0] for row in cur.fetchall()]


# -- reads ------------------------------------------------------------------------------------------

def list_ledger(conn, tenant_id: str = None, window_start=None, limit: int = 200) -> list:
    where, params = [], []
    if tenant_id:
        where.append("tenant_id = %s")
        params.append(tenant_id)
    if window_start is not None:
        where.append("window_start = %s")
        params.append(window_start)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, tenant_id, kind, ref_id, modality, gpu_seconds, window_start, ts "
            "FROM usage_ledger" + clause + " ORDER BY id DESC LIMIT %s", (*params, int(limit)))
        return [{"id": r[0], "tenant_id": r[1], "kind": r[2], "ref_id": r[3], "modality": r[4],
                 "gpu_seconds": float(r[5]), "window_start": r[6], "ts": r[7]}
                for r in cur.fetchall()]


def reconciliation(conn, tenant_id: str = None) -> dict:
    """T631's identity: the ledger total equals the sum of settled reservations.

    Exposed as a query rather than only asserted in a test so an operator can check it against a
    live database — a reconciliation that only holds in CI is not a reconciliation.
    """
    where, params = "", []
    if tenant_id:
        where = " WHERE tenant_id = %s"
        params = [tenant_id]
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(gpu_seconds), 0) FROM usage_ledger" + where, params)
        ledger_total = float(cur.fetchone()[0])
        cur.execute("SELECT coalesce(sum(settled_gpu_seconds), 0) FROM usage_reservation"
                    + (where + " AND " if where else " WHERE ") + "state = 'settled'", params)
        settled_total = float(cur.fetchone()[0])
    return {"ledger_total": ledger_total, "settled_reservation_total": settled_total,
            "reconciled": abs(ledger_total - settled_total) < 1e-9}
