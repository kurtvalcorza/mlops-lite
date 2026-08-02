"""Bounded admission decision ring with server-composed explanations (027 T709).

**A decision history, not a queue** (research R1). Admission *decides immediately* — it admits,
refuses, or evicts and retries — so there is no pending state to report and no queue position to
show. Shipping a "queue" view would be the same fake-semantics error the spec refuses elsewhere: an
interface element implying a mechanism the system does not have. Every record here is a decision that
already happened, stamped with `decided_at`, which is a decision time and never a queue age.

## Why the explanation is composed here

The interface must not compose its own wording, or it drifts from admission's real reasoning. The
templates live next to the values the decision used, so an explanation cannot claim a bound that was
not the deciding one.

## The two checks are never collapsed

`budget` bounds the **accounted set** (`accounted + requested ≤ usable_budget`); `live-vram` bounds
the **incoming load** (`requested + headroom ≤ live_free`). `live_free` already excludes current
residents, so summing the resident set against it double-counts them — the v1.6.0 defect v1.6.1
corrected. Reproducing that conflation here would misreport *why* a model was refused, and the two
failures have opposite remedies: eviction fixes a budget failure and does nothing for a live-VRAM one.

`cannot-fit-alone` is distinguished from `budget` for the same reason: the former is **structural**
(no amount of eviction or waiting helps), the latter **transient**.

## Cost discipline

The ring append performs **no IO** and does not extend any critical section. It is a deque append
under its own small lock, called by `acquire()` as it returns — never while the admission lock is
held across anything, and never in a way that can fail an admission. An observability path that can
refuse a request is worse than no observability path.
"""
import itertools
import threading
import time
from collections import deque

#: Default ring size. Large enough that an operator investigating a contention event sees the whole
#: burst that produced it; small enough to be irrelevant to the idle footprint budget.
DEFAULT_CAPACITY = 64

_ids = itertools.count(1)


def _iso(epoch=None) -> str:
    import datetime
    when = datetime.datetime.fromtimestamp(epoch if epoch is not None else time.time(),
                                           datetime.timezone.utc)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _gb(value):
    return None if value is None else round(float(value), 2)


def _fmt(value) -> str:
    return "?" if value is None else f"{float(value):.1f}"


def explain(record: dict) -> str:
    """Compose the human-readable explanation from the same values the decision used (FR-378).

    The wording deliberately teaches the platform's rules at the moment they bite — that a job takes
    the whole GPU and is never preempted, and that serving models share a bounded budget — because
    that is the moment an operator is actually motivated to learn them.
    """
    decision, reason = record.get("decision"), record.get("reason")
    requested = _fmt(record.get("requested_gb"))
    budget = _fmt(record.get("usable_budget_gb"))

    if decision == "refused" and reason == "job-exclusive":
        return (f"Refused: job {record.get('blocking_tenant') or 'unknown'} holds the GPU "
                f"exclusively. A running job is never preempted.")
    if decision == "refused" and reason == "budget":
        total = (record.get("accounted_resident_gb") or 0) + (record.get("requested_gb") or 0)
        return (f"Refused: admitting {requested} GB would take the resident set to {_fmt(total)} GB, "
                f"over the {budget} GB usable budget.")
    if decision == "refused" and reason == "live-vram":
        return (f"Refused: needs {requested} GB plus {_fmt(record.get('headroom_gb'))} GB headroom; "
                f"live free VRAM is {_fmt(record.get('live_free_gb'))} GB on device "
                f"{record.get('device_index', 0)}.")
    if decision == "refused" and reason == "cannot-fit-alone":
        return (f"Refused: {requested} GB exceeds the {budget} GB usable budget even with the GPU "
                f"empty. Evicting other models cannot help.")
    if decision == "refused" and reason == "load-failed":
        return (f"Refused: {record.get('model_key')} was admitted but failed to load "
                f"({record.get('detail') or 'no detail'}). Its reserved capacity has been released.")
    if decision == "evicted-retry":
        evicted = record.get("evicted") or []
        keys = ", ".join(e.get("model_key", "?") for e in evicted) or "nothing"
        policy = (evicted[0].get("policy") if evicted else None) or "idle-first"
        freed = sum(e.get("freed_gb") or 0 for e in evicted)
        return (f"Attempt {record.get('attempt', 1)}: evicted {keys} ({policy}) to free "
                f"{_fmt(freed)} GB; both bounds will be re-derived on the next attempt.")
    if decision == "admitted":
        n = len(record.get("residents") or [])
        return (f"Admitted to device {record.get('device_index', 0)}: fits the usable budget and "
                f"live free VRAM alongside {n} resident model(s).")
    return f"{decision or 'unknown'}: no explanation template matched."


class AdmissionLog:
    """The bounded ring. Thread-safe, IO-free, and never able to fail an admission."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, clock=time.time):
        self.capacity = capacity
        self._clock = clock
        self._records = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, **fields) -> dict:
        """Append one decision. Returns the stored record (with `id` and `explanation` filled in).

        Swallows nothing: a malformed record is still stored, because a record the observability
        layer refused to keep is exactly the one an incident review needed.
        """
        entry = {
            "id": f"adm_{next(_ids):08d}",
            "op_id": fields.get("op_id"),
            "tenant": fields.get("tenant"),
            "kind": fields.get("kind", "serving"),
            # The RESOURCE being admitted. Keyed by model instance, not tenant: many tenants
            # requesting the same model share one resident child whose ref-count rises, and keying
            # by tenant would render five tenants on one model as five residents each apparently
            # holding its own VRAM.
            "model_key": fields.get("model_key"),
            "requested_gb": _gb(fields.get("requested_gb")),
            "decision": fields.get("decision"),
            "reason": fields.get("reason"),
            "attempt": fields.get("attempt"),
            "residents": fields.get("residents") or [],
            "evicted": fields.get("evicted") or [],
            "usable_budget_gb": _gb(fields.get("usable_budget_gb")),
            "accounted_resident_gb": _gb(fields.get("accounted_resident_gb")),
            "reserved_gb": _gb(fields.get("reserved_gb")),
            "unmaterialized_gb": _gb(fields.get("unmaterialized_gb")),
            "live_free_gb": _gb(fields.get("live_free_gb")),
            "headroom_gb": _gb(fields.get("headroom_gb")),
            "device_index": fields.get("device_index", 0),
            "blocking_tenant": fields.get("blocking_tenant"),
            "detail": fields.get("detail"),
            "decided_at": _iso(self._clock()),
        }
        entry["explanation"] = explain(entry)
        with self._lock:
            self._records.append(entry)
        return entry

    def records(self, limit: int = None) -> list:
        """Most recent first — an operator investigating a refusal wants the refusal, not the
        history that preceded it by an hour."""
        with self._lock:
            out = list(self._records)
        out.reverse()
        return out[:limit] if limit else out

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def snapshot_from_coordinator(coordinator, log: AdmissionLog, limit: int = None) -> dict:
    """The `GET /runtime/admission` body: current bounds + the decision history.

    Both reservation terms are reported separately because the two checks do not take the same one:
    the budget check counts **every** outstanding reservation, while the live-VRAM check deducts only
    the **not-yet-materialized** ones — a reservation whose bytes are already allocated is visible in
    `live_free` itself, and subtracting it twice would misreport the headroom.
    """
    gib = 1024 ** 3
    snap = coordinator.snapshot()
    vram = snap.get("vram") or {}
    return {
        "observed_at": _iso(),
        "residents": [{"model_key": r["model"], "kind": "serving", "vram_gb": _gb(r[
            "vram_accounted_bytes"] / gib), "state": r["state"].replace("_", "-"),
            "active_requests": r["active_requests"]} for r in snap.get("resident", [])],
        "usable_budget_gb": _gb((vram.get("usable_capacity") or 0) / gib),
        "accounted_resident_gb": _gb((vram.get("accounted") or 0) / gib),
        "reserved_gb": _gb((vram.get("reserved") or 0) / gib),
        "unmaterialized_gb": _gb((vram.get("unmaterialized") or 0) / gib),
        "live_free_gb": _gb((vram.get("live_free") or 0) / gib),
        "headroom_gb": _gb((vram.get("safety_headroom") or 0) / gib),
        "job_barrier": snap.get("job_barrier", False),
        "active_job": snap.get("active_job"),
        "capacity": log.capacity,
        "records": log.records(limit),
    }
