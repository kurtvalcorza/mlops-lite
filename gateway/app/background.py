"""Retained fire-and-forget background work (018 US1, FR-164 — review §4.7).

The routers' off-path logging used bare `asyncio.ensure_future(...)` with no reference retained —
a documented CPython foot-gun: the event loop holds only a weak reference, so a detached task can
be garbage-collected mid-flight and its exception silently vanishes. `spawn()` keeps a strong
reference until the task completes, and anything that does NOT complete (scheduling failure,
in-task exception, cancellation) increments a visible dropped-work counter: background work
either completes or is *counted*, never silently discarded. Fail-open is preserved — `spawn()`
itself never raises into the serving path.
"""
import asyncio

from prometheus_client import Counter

DROPPED_WORK = Counter(
    "gateway_dropped_work_total",
    "Background work (prediction logs / captures) that did not complete",
    ["kind", "reason"],
)

_TASKS: set = set()  # strong references — released by the done-callback


def spawn(coro, kind: str):
    """Schedule `coro` as a retained background task. Returns the task, or None if scheduling
    failed (counted as dropped, never raised — the served response must not be affected)."""
    try:
        task = asyncio.ensure_future(coro)
    except Exception:
        coro.close()  # don't leak an un-awaited coroutine
        DROPPED_WORK.labels(kind=kind, reason="not_scheduled").inc()
        return None
    _TASKS.add(task)

    def _done(t):
        _TASKS.discard(t)
        if t.cancelled():
            DROPPED_WORK.labels(kind=kind, reason="cancelled").inc()
        elif t.exception() is not None:
            DROPPED_WORK.labels(kind=kind, reason="error").inc()

    task.add_done_callback(_done)
    return task
