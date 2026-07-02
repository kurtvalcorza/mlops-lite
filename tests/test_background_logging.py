"""018 US1 — retained background logging work (T348, FR-164 — review §4.7).

Offline, GPU-free: drives `gateway/app/background.py` directly. Pre-018 the routers used bare
`asyncio.ensure_future` with no reference retained — a task could be garbage-collected mid-flight
and its prediction log silently vanish. `spawn()` must (a) hold a strong reference until the task
completes, and (b) count anything that does NOT complete (scheduling failure, in-task exception,
cancellation) in the `gateway_dropped_work_total` metric — completed-or-counted, never silent.
"""
import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import background  # noqa: E402


def _dropped(kind, reason):
    return background.DROPPED_WORK.labels(kind=kind, reason=reason)._value.get()


def test_completed_task_is_not_counted_dropped():
    before = _dropped("t-ok", "error")
    done = []

    async def work():
        done.append(True)

    async def main():
        task = background.spawn(work(), kind="t-ok")
        assert task in background._TASKS          # strong reference retained while pending
        await task
        await asyncio.sleep(0)                    # let the done-callback run
        assert task not in background._TASKS      # released after completion

    asyncio.run(main())
    assert done == [True]
    assert _dropped("t-ok", "error") == before


def test_failing_task_is_counted_dropped():
    before = _dropped("t-err", "error")

    async def boom():
        raise RuntimeError("store down")

    async def main():
        task = background.spawn(boom(), kind="t-err")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert _dropped("t-err", "error") == before + 1


def test_cancelled_task_is_counted_dropped():
    before = _dropped("t-cancel", "cancelled")

    async def forever():
        await asyncio.sleep(60)

    async def main():
        task = background.spawn(forever(), kind="t-cancel")
        await asyncio.sleep(0)                    # let it start
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert _dropped("t-cancel", "cancelled") == before + 1


def test_scheduling_failure_is_counted_not_raised():
    # No running event loop → ensure_future raises inside spawn; the serving path must see None
    # (fail-open), the metric must see the drop, and the un-awaited coroutine must be closed.
    before = _dropped("t-sched", "not_scheduled")

    async def work():  # pragma: no cover — never runs
        pass

    assert background.spawn(work(), kind="t-sched") is None
    assert _dropped("t-sched", "not_scheduled") == before + 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
