"""Web-free LLM go-live ordering (024 US2, T574 — data-model.md §go_live).

The promote route is the ONE operator go-live surface (022 FR-255): gate → resolve → move the alias →
persist the pointer → agent reload, in a strict order (FR-265: an unresolvable target is refused
BEFORE the alias moves; a conflicting in-flight activation is refused before the alias moves; on
success the prior pointer is captured before it is overwritten). That ordering used to live INSIDE the
FastAPI handler, tangled with `HTTPException`/metric emission, so it could only be exercised through the
HTTP stack. Here it becomes a pure use-case over injected `registry`/`activation` seams that returns a
`GoLiveResult` — the router (`routers/models.py:promote`) is reduced to a translate→call→map adapter,
and the sequence unit-tests offline with fakes (`tests/test_promotion_ordering.py`), byte-identical to
the live route (contracts/preservation.md §C2).

This module imports NO `fastapi`/`httpx`: the HTTP status + `REGISTRY_OPS` label emission stay in the
router; `go_live` only decides the outcome, the response body, and the ordered metric labels.

Single-caller invariant (FR-336/275/307/313, SC-170): `go_live` is called ONLY by the operator promote
route. The scheduler/one-click policy paths keep calling `registry.promote` directly, so they can gate
a candidate but can never live-switch the served LLM.
"""
import enum


class GoLiveOutcome(enum.Enum):
    """The terminal shape of a go-live attempt (data-model.md §Router mapping). The HTTP status is a
    function of the outcome; the body + the ordered `REGISTRY_OPS` labels ride the `GoLiveResult`."""
    NOT_FOUND = "not_found"    # 404, no metric — the pre-metric missing-version 404
    REFUSED = "refused"        # 409, `refused` — unresolvable adapter, BEFORE the alias moves (FR-265)
    CONFLICT = "conflict"      # 409, `conflict` (pre-alias) OR `ok`+`conflict` (post-promote TOCTOU)
    BLOCKED = "blocked"        # 200 promoted:false, `blocked` — the gate held the alias put
    PROMOTED = "promoted"      # 200 promoted:true, `ok` (+`unresolvable` on rollback)
    ERROR = "error"            # 502 — pre-check RegistryError (no metric) OR promote-time (`error`)


#: outcome → HTTP status (the router applies this; kept here so the mapping has one home).
_HTTP_STATUS = {
    GoLiveOutcome.NOT_FOUND: 404,
    GoLiveOutcome.REFUSED: 409,
    GoLiveOutcome.CONFLICT: 409,
    GoLiveOutcome.BLOCKED: 200,
    GoLiveOutcome.PROMOTED: 200,
    GoLiveOutcome.ERROR: 502,
}


class GoLiveResult:
    """The decision the router applies: the `outcome`, the `body` (returned as-is on 200, or `{detail}`
    for an error status), and `metric_statuses` — the `REGISTRY_OPS` `status` labels to emit IN ORDER
    (usually one; a PROMOTED-then-rolled-back activation emits `["ok", "unresolvable"]`, and the
    post-promote TOCTOU conflict emits `["ok", "conflict"]` — a list so neither increment is dropped)."""

    def __init__(self, outcome: GoLiveOutcome, *, body: dict, metric_statuses=()):
        self.outcome = outcome
        self.body = body
        self.metric_statuses = tuple(metric_statuses)

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.outcome]

    @property
    def ok(self) -> bool:
        return self.http_status == 200


def go_live(name: str, version, *, override: bool, preempt: bool, registry, activation) -> GoLiveResult:
    """Decide the go-live outcome for `name@version`, in the FR-265 order, over the injected
    `registry`/`activation` seams (the real modules in production; fakes offline). Returns a
    `GoLiveResult`; raises nothing HTTP-specific — the router maps outcome→status and emits the metrics.

    Ordering (each step gates the next):
      1. version existence          — missing ⇒ NOT_FOUND (404, no metric), before anything moves;
      2. LLM target resolution      — an unresolvable adapter ⇒ REFUSED (409), BEFORE `registry.promote`;
      3. pre-alias conflict check   — an in-flight activation ⇒ CONFLICT (409), before the alias moves;
      4. the gated promote          — moves the alias; a gate block ⇒ BLOCKED (200, promoted:false);
      5. durable activation         — capture the prior pointer BEFORE it is overwritten, then activate;
                                      a post-promote conflict emits `ok` THEN `conflict` (alias left
                                      moved); an unresolvable reload emits `ok` THEN `unresolvable`.
    """
    # 1 + 2 — pre-checks that fail-loud to 502 with NO metric (before any REGISTRY_OPS increment).
    try:
        versions = registry.list_versions(name)
    except registry.RegistryError as e:
        return GoLiveResult(GoLiveOutcome.ERROR, body={"detail": f"registry error: {e}"})
    if not any(v["version"] == str(version) for v in versions):
        return GoLiveResult(GoLiveOutcome.NOT_FOUND,
                            body={"detail": f"{name!r} has no version {version}"})
    try:
        llm_target = registry.llm_target_info(name, version)
    except registry.RegistryError as e:
        return GoLiveResult(GoLiveOutcome.ERROR, body={"detail": f"registry error: {e}"})
    if llm_target and llm_target.get("error"):
        # An adapter whose base does not resolve is refused HERE, before the gate — the alias and the
        # currently-served LLM stay unchanged (FR-265).
        return GoLiveResult(GoLiveOutcome.REFUSED,
                            body={"detail": f"promotion refused: {llm_target['error']}"},
                            metric_statuses=["refused"])
    # 3 — refuse a conflicting in-flight activation BEFORE promote moves the @serving alias, so the
    # registry never names a version we never activated.
    if llm_target:
        try:
            activation.service().assert_no_conflict(name, version)
        except activation.ActivationError as e:
            return GoLiveResult(GoLiveOutcome.CONFLICT,
                                body={"detail": f"activation refused: {e}"},
                                metric_statuses=["conflict"])
    # 4 — the gated promote (moves the alias). A promote-time RegistryError emits `error` (unlike the
    # pre-checks above, which return 502 before any metric).
    try:
        res = registry.promote(name, version, override=override)
    except registry.RegistryError as e:
        return GoLiveResult(GoLiveOutcome.ERROR, body={"detail": f"registry error: {e}"},
                            metric_statuses=["error"])
    if not res.get("promoted", True):
        return GoLiveResult(GoLiveOutcome.BLOCKED, body=res, metric_statuses=["blocked"])
    # 5 — promoted: the alias moved. Emit `ok`; for a text-generation target run the durable activation.
    metrics = ["ok"]
    if llm_target and res.get("promoted"):
        prior = registry.get_serving_llm()  # capture BEFORE the pointer moves (FR-265 rollback)
        try:
            sl = activation.service().activate(
                name=name, version=version, prior=prior, preempt=preempt,
                kind=llm_target["kind"], base=llm_target["base"])
        except activation.ActivationError as e:
            # Post-promote TOCTOU: the alias already moved and `ok` already fired; a conflicting op
            # started between the pre-check and submit ⇒ emit `conflict` too, 409, alias left moved.
            metrics.append("conflict")
            return GoLiveResult(GoLiveOutcome.CONFLICT,
                                body={"detail": f"activation refused: {e}"},
                                metric_statuses=metrics)
        if sl.get("rolled_back") is not None:
            metrics.append("unresolvable")
        res["serving_llm"] = sl
    return GoLiveResult(GoLiveOutcome.PROMOTED, body=res, metric_statuses=metrics)
