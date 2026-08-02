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
import datetime


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
    """The envelope for a single-source read that has no join to track."""
    return {"data": data, "observed": observed or {"gateway": utcnow()},
            "degraded": list(degraded or []), "conflict": conflict}
