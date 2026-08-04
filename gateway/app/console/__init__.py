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

#: **Single flight per read.** At most one thread is outstanding for any given (source, function)
#: pair; while one is still running, a second request for the same read is refused immediately and
#: reported degraded.
#:
#: This is the load-bearing bound, and it replaced a global cap that did not survive contact with
#: reality. The global version assumed abandoned reads drain quickly. They do not: boto3 and the
#: MLflow client retry against an unresolvable host for **minutes**, so a console polling every few
#: seconds spawned a fresh stuck thread per source per poll and saturated any fixed cap within a
#: minute — at which point *every* source read was refused, including the healthy ones.
#:
#: Single flight makes the thread count bounded by the number of distinct reads (a handful) rather
#: than by the poll rate, and a source that recovers frees its own slot on the next answer without
#: waiting for anything else.
_inflight = set()
_inflight_lock = threading.Lock()


def _run_detached(key, fn, future, loop):
    try:
        result, error = fn(), None
    except BaseException as e:  # noqa: BLE001 — delivered to the awaiting coroutine
        result, error = None, e
    finally:
        with _inflight_lock:
            _inflight.discard(key)
    # The awaiting coroutine may already have timed out and moved on; `call_soon_threadsafe` on a
    # closed loop raises, and setting a result on a cancelled future is a no-op we simply skip.
    try:
        loop.call_soon_threadsafe(
            lambda: None if future.done() else
            (future.set_exception(error) if error else future.set_result(result)))
    except RuntimeError:
        pass


def _read_key(name, fn):
    """One key per distinct read, not per source.

    Keying on the source alone would make `store`'s job listing and its unlabeled count block each
    other even though they are separate queries against a healthy database. Keying on the callable's
    definition site separates them while still collapsing repeat polls of the *same* read, which is
    the thing that multiplies.
    """
    return f"{name}:{getattr(fn, '__qualname__', repr(fn))}"


def _spawn(key, fn):
    """Run `fn` on a **daemon** thread, returning an asyncio future for its result.

    A `ThreadPoolExecutor` would be the obvious choice and is the wrong one here. Its workers are
    non-daemon and joined by an `atexit` hook, so a single read stuck against a sick backend — the
    exact case this bounding exists for — holds the whole process open at shutdown. That turns a
    degraded backend into a gateway that will not stop, which is a worse failure than the one the
    timeout was added to prevent.

    A timed-out read's thread cannot be cancelled; it runs until its own client gives up. Detaching
    it is therefore the only option, and single flight is what keeps a hung backend from growing one
    abandoned thread per poll.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    with _inflight_lock:
        if key in _inflight:
            raise RuntimeError(f"a read of {key} is already outstanding")
        _inflight.add(key)
    threading.Thread(target=_run_detached, args=(key, fn, future, loop), daemon=True,
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

        Single flight applies: if this exact read is already outstanding from an earlier request,
        this one is refused immediately and reported degraded rather than starting a second thread
        against the same sick backend.
        """
        try:
            value = await asyncio.wait_for(_spawn(_read_key(name, fn), fn),
                                           timeout_s or SOURCE_TIMEOUT_S)
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
