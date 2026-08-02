"""Console read-projection layer (027 — contracts/console-read-api.md).

Every route in `gateway/app/routers/console.py` returns the **envelope** built here:

    { "data": …, "observed": {"<source>": iso8601}, "degraded": ["agent"], "conflict": … }

Three rules make the envelope worth having, and each is a rule the console cannot enforce for itself:

  * **A partially-degraded projection returns 200** with the reachable parts populated and the
    unreachable parts `null` (FR-428). It must not fail whole — a console that 503s because one of
    five sources is down tells an operator nothing about the four that are up, at exactly the moment
    they need it.

  * **`null` means unknown and is never serialized as `0` or `[]`.** This is the single most
    consequential rule in the contract. An unreachable agent rendering as "0 devices" is not a
    degraded reading, it is a **false** one, and an operator acting on it (there is no GPU, so
    nothing is running) would be acting on a fabrication. `degraded` names which sources produced the
    nulls so the interface can say "unknown" rather than guess.

  * **`observed` timestamps are per source**, because a projection joining five backends has five
    different data ages and one page-level "as of" would be a lie about four of them.

Conflicts are computed **per observation, never persisted** (research R9): a conflict is a statement
about two readings taken at particular moments, and storing it would outlive the disagreement.
"""
import asyncio
import datetime
import os
import threading

#: How long one source read may take before the projection gives up on it and reports it degraded.
#:
#: Every source behind these routes is a network call to something that can be sick rather than
#: down — a tracking server swapping, an object store with a half-open socket — and a sick backend
#: hangs rather than refusing. Without a bound, one such source holds the whole console request
#: open, which is worse than the degradation it is trying to avoid reporting: the operator gets a
#: spinner instead of "registry unreachable". A read that does not answer in this long IS a
#: degraded source, and saying so is the honest outcome.
SOURCE_TIMEOUT_S = float(os.getenv("CONSOLE_SOURCE_TIMEOUT_S", "5"))

#: How many source reads may be outstanding at once. Past this, a read is refused immediately and
#: reported degraded rather than queued: the console polls, and queueing behind a hung backend just
#: converts one slow source into a slow console.
MAX_INFLIGHT = 8

_inflight = 0
_inflight_lock = threading.Lock()


def _run_detached(fn, future, loop):
    global _inflight
    try:
        result, error = fn(), None
    except BaseException as e:  # noqa: BLE001 — delivered to the awaiting coroutine
        result, error = None, e
    finally:
        with _inflight_lock:
            _inflight -= 1
    # The awaiting coroutine may already have timed out and moved on; `call_soon_threadsafe` on a
    # closed loop raises, and setting a result on a cancelled future is a no-op we simply skip.
    try:
        loop.call_soon_threadsafe(
            lambda: None if future.done() else
            (future.set_exception(error) if error else future.set_result(result)))
    except RuntimeError:
        pass


def _spawn(fn):
    """Run `fn` on a **daemon** thread, returning an asyncio future for its result.

    A `ThreadPoolExecutor` would be the obvious choice and is the wrong one here. Its workers are
    non-daemon and joined by an `atexit` hook, so a single read stuck against a sick backend — the
    exact case this bounding exists for — holds the whole process open at shutdown. That turns a
    degraded backend into a gateway that will not stop, which is a worse failure than the one the
    timeout was added to prevent.

    A timed-out read's thread cannot be cancelled; it runs until its own client gives up. Detaching
    it is therefore the only option, and `MAX_INFLIGHT` is what keeps a permanently hung backend
    from growing one abandoned thread per poll.
    """
    global _inflight
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    with _inflight_lock:
        if _inflight >= MAX_INFLIGHT:
            raise RuntimeError("too many console source reads outstanding")
        _inflight += 1
    threading.Thread(target=_run_detached, args=(fn, future, loop), daemon=True,
                     name="console-read").start()
    return future


def utcnow() -> str:
    """The observation timestamp format the contract publishes: UTC, second precision, `Z`.

    Second precision on purpose — these are wall-clock observation stamps a human reads as data age,
    and sub-second digits imply a precision the 1-second-TTL caches underneath do not have.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


class Projection:
    """Accumulates a multi-source read, tracking what was observed and what was unreachable.

    Used as a builder rather than a return value so a join can record each source's outcome at the
    point it happens::

        p = Projection()
        devices = p.source("agent", lambda: runtime.devices())      # None + degraded on failure
        models  = p.source("registry", registry.list_models)
        return p.envelope({"devices": devices, "models": models})

    A source that raises is recorded as degraded and yields `None`; the caller composes the payload
    with whatever it got. That shape is what makes "populate the reachable parts" the default
    behaviour rather than something each route has to remember.
    """

    def __init__(self):
        self.observed = {}
        self.degraded = []
        self.conflicts = []

    def source(self, name: str, fn, default=None):
        """Read one source. Records `observed[name]` on success, `degraded` on any failure."""
        try:
            value = fn()
        except Exception:  # noqa: BLE001 — any source failure is degradation, never a 500
            if name not in self.degraded:
                self.degraded.append(name)
            return default
        self.observed[name] = utcnow()
        return value

    async def read(self, name: str, fn, default=None, timeout_s: float = None):
        """`source()` for an async route: runs the blocking read off the loop, under a deadline.

        Same contract as `source()` — success records `observed[name]`, any failure records
        `degraded` and yields `default` — with two additions the sync version cannot offer: the
        blocking client does not stall the event loop, and a source that never answers is reported
        as degraded instead of hanging the request.

        Call several of these under `asyncio.gather` so a projection's sources are read
        concurrently; four sequential five-second timeouts would make a fully-degraded console take
        twenty seconds to say it knows nothing.
        """
        try:
            value = await asyncio.wait_for(_spawn(fn), timeout_s or SOURCE_TIMEOUT_S)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 — incl. TimeoutError
            self.mark_degraded(name)
            return default
        self.observed[name] = utcnow()
        return value

    def mark_degraded(self, name: str) -> None:
        if name not in self.degraded:
            self.degraded.append(name)

    def mark_observed(self, name: str) -> None:
        self.observed[name] = utcnow()

    def conflict(self, field: str, values: dict, note: str = "") -> None:
        """Record a `StateConflict`: two sources disagreeing about one field.

        `values` maps source -> what it reported. Disagreement is reported, never resolved by
        precedence: picking a winner would hide the fact that two systems of record are out of step,
        which is the thing an operator most needs to know (research R9).
        """
        self.conflicts.append({"field": field, "values": values, "note": note,
                               "observed_at": utcnow()})

    def envelope(self, data) -> dict:
        return {"data": data, "observed": dict(self.observed),
                "degraded": list(self.degraded),
                "conflict": self.conflicts or None}


def envelope(data, *, observed=None, degraded=None, conflict=None) -> dict:
    """The envelope for a single-source read that has no join to track.

    `observed=None` means "no source was named, stamp it as the gateway's own read"; `observed={}`
    means **nothing was observed** and must stay empty. The distinction matters: a fully-degraded
    read that carried a gateway timestamp would show the console a data age for data it never got,
    which is the same class of falsehood as rendering `null` as `0`.
    """
    return {"data": data,
            "observed": {"gateway": utcnow()} if observed is None else dict(observed),
            "degraded": list(degraded or []), "conflict": conflict}
