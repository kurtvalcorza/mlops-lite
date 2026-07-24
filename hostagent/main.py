#!/usr/bin/env python3
"""GPU host agent HTTP surface (018 US2, T352 — contracts/agent-api.md; 020 US3, T413).

The single stable endpoint (`platformlib.topology.AGENT_PORT`, :8100). The agent serves the read
surface (health/metrics/engines — open, like the retired per-daemon probes), the control surface
(`POST /control/unload`, opt-in `X-Agent-Control` secret, research R6), engine inference
passthrough (`/engines/<id>/<verb>`, + the byte-compatible legacy paths, FR-177), and the jobs
surface (`/jobs` + the legacy trainer aliases). T364 retired the last legacy daemon and the
lockfile interop shim: admission is the single in-process authority for the one GPU tenant.

020 T413 (FR-205/206) put the route table in the transport-neutral `handle_get`/`handle_post`
routers below and ran a dual-runtime drill (stdlib vs uvicorn-ASGI); the verdict was keep-stdlib
(docs/on-hardware-validation.md), so 023 US6 (T535, FR-315) deleted `hostagent/asgi.py` and the
`AGENT_RUNTIME` switch: ONE transport, the stdlib `BoundedAgentServer` below — authenticated
(023 US2), with deterministic worker/queue/body/timeout/shutdown bounds (FR-316..320). The agent
stays pip-dep-free.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from hostagent import admission as adm  # noqa: E402
from hostagent import auth as agent_auth  # noqa: E402
from hostagent import jobs as jobs_mod  # noqa: E402
from hostagent import lifecycle  # noqa: E402
from hostagent import swap as swap_mod  # noqa: E402
from hostagent.journal import Journal  # noqa: E402
from hostagent.metrics import REGISTRY  # noqa: E402
from platformlib.store import StoreError  # noqa: E402
from platformlib.topology import AGENT_PORT, STATE_DIR, vram_budget_gb  # noqa: E402

VRAM_GB = vram_budget_gb()  # 020 US4 (FR-207): single shared resolver, no duplicated default
# Codex review (018): the gateway + Prometheus reach the agent via host.docker.internal / an
# injected WSL IP — a loopback-only bind would make AGENT_URL unreachable from the containers the
# moment a fold-in flips traffic here. Default matches the legacy daemons; override to tighten.
AGENT_BIND = os.getenv("AGENT_BIND", "0.0.0.0")

# --- 023 US6 (T531..T533, FR-316..320): deterministic transport bounds ------------------------------
# Safe defaults for the single-operator workload (contracts/agent-security.md §Request bounds);
# every knob is env-overridable, and the [HW] drill (T536) validates/lowers them on the target box.
AGENT_MAX_WORKERS = int(os.getenv("AGENT_MAX_WORKERS", "8"))          # concurrent handlers
AGENT_QUEUE_SIZE = int(os.getenv("AGENT_QUEUE_SIZE", "8"))            # admitted-but-waiting bound
AGENT_QUEUE_WAIT_S = float(os.getenv("AGENT_QUEUE_WAIT_S", "5"))      # max wait for a worker slot
AGENT_IO_TIMEOUT_S = float(os.getenv("AGENT_IO_TIMEOUT_S", "30"))     # per-socket read/write
AGENT_SHUTDOWN_TIMEOUT_S = float(os.getenv("AGENT_SHUTDOWN_TIMEOUT_S", "10"))  # drain budget
AGENT_MAX_JSON_BYTES = int(os.getenv("AGENT_MAX_JSON_BYTES", str(1 << 20)))         # 1 MiB
AGENT_MAX_MULTIPART_BYTES = int(os.getenv("AGENT_MAX_MULTIPART_BYTES", str(32 << 20)))  # 32 MiB
# `SWAP_CONTROL_SECRET` is still accepted as a fallback (a deployment that set it pre-018 keeps
# working) but the agent's state-changing routes standardize on the agent-control secret.
CONTROL_SECRET = os.getenv("AGENT_CONTROL_SECRET") or os.getenv("SWAP_CONTROL_SECRET", "")

_started_at = time.time()
_interrupted_at_start = 0


def build_agent():
    """Wire the agent's components. Admission is the single in-process GPU authority (T364 retired
    the cross-process lockfile shim)."""
    admission = adm.Admission(vram_budget_gb=VRAM_GB)
    journal = Journal()  # US4 T375-B: durable job state lives in the Postgres `jobs` table now
    from hostagent import adapters  # one runtime per registered engine adapter (T358+)

    manager = lifecycle.EngineManager(admission, runtimes=adapters.build_runtimes(admission))
    jobs = jobs_mod.JobManager(admission, journal)  # 018 T362: the trainer folded in
    return admission, journal, manager, jobs


def _engine_error_status(exc: BaseException) -> int:
    """Map a lifecycle/admission failure to the preserved 008–017 error vocabulary."""
    if isinstance(exc, (adm.Held, swap_mod.PreemptRefused, swap_mod.SwapError)):
        return 409  # slot held / preempt refused (job holder) / swap could not evict
    if isinstance(exc, adm.VramExceeded):
        return 507
    if isinstance(exc, lifecycle.EngineError):
        return 503  # disabled / unavailable / never-ready
    if isinstance(exc, ValueError):
        return 400  # bad body / unknown verb reaching the adapter
    return 502      # child/backend inference failure


def error_response(exc: BaseException) -> tuple:
    """(status, payload) for an engine-path failure — one mapping for both transports (T413).
    `adm.Held` keeps its holder/kind fields (the 008–017 vocabulary)."""
    if isinstance(exc, adm.Held):
        return 409, {"error": str(exc), "holder": exc.holder.get("tenant"),
                     "kind": exc.holder.get("kind")}
    return _engine_error_status(exc), {"error": str(exc)}


def _admit(manager, rt, engine_id: str, preempt: bool, batch_active_fn=None) -> float:
    """Return cold-start ms, making room for the engine. `preempt=true` (T363: the gateway swap
    thinned to just forwarding this flag) runs the agent's transactional evict→admit — the single
    admission lock is the authority now, a `kind="job"` holder is refused structurally (FR-172), and
    a GPU batch's serving holder is refused via `batch_active_fn` (FR-155 — the structural
    replacement for the deleted gateway batch probe, read fresh at the decision point). No network
    probe. Called under the runtime lock so it can't be reaped before the forward."""
    if preempt:
        return swap_mod.preempt_for(manager, engine_id, batch_active_fn=batch_active_fn)["load_ms"]
    return rt.ensure_loaded()


def _forward_under_lease(manager, engine_id: str, verb: str, do_forward, *, multipart=False,
                         preempt=False, batch_active_fn=None):
    """Shared lifecycle wrapper for an engine forward (contracts/agent-api.md): resolve, verb-check,
    ensure-admitted (cold-load, or a `preempt=true` swap) UNDER the runtime lock, delegate the
    engine-specific call, stamp `last_used`. Returns (status_code, payload). Framework-free so it is
    unit-testable without HTTP. The lock is held across the whole forward so the reaper/swap cannot
    unload mid-flight (the supervisor's `_lock`-across-generation semantics, now shared).
    `do_forward(adapter, load_ms)` is the JSON or multipart call."""
    rt = manager.runtimes.get(engine_id)
    if rt is None:
        return 404, {"error": f"unknown engine {engine_id!r}"}
    if verb not in getattr(rt.adapter, "verbs", ()):
        return 404, {"error": f"engine {engine_id!r} has no verb {verb!r}"}
    # Symmetric content-type guard (claude review, T360): a multipart POST to a JSON-only engine AND
    # a JSON POST to a multipart-only engine (e.g. vision, which has no `forward`) both get a clean
    # 415 rather than the latter falling through to an AttributeError → 502.
    method = "forward_multipart" if multipart else "forward"
    if not hasattr(rt.adapter, method):
        kind = "multipart" if multipart else "JSON"
        return 415, {"error": f"engine {engine_id!r} does not accept {kind} for verb {verb!r}"}
    try:
        with rt.lock:
            load_ms = _admit(manager, rt, engine_id, preempt, batch_active_fn)
            result = do_forward(rt.adapter, load_ms)
            rt.touch()
        return 200, result
    except Exception as e:  # noqa: BLE001 — mapped to the preserved status vocabulary
        return error_response(e)


def forward_engine(manager, engine_id: str, verb: str, body: dict, *, preempt=False,
                   batch_active_fn=None):
    """JSON inference passthrough (llm/asr/embed/tabular): forward a parsed dict body."""
    return _forward_under_lease(
        manager, engine_id, verb,
        lambda ad, load_ms: ad.forward(verb, body, load_ms),
        preempt=preempt, batch_active_fn=batch_active_fn)


def forward_engine_multipart(manager, engine_id: str, verb: str, raw_body: bytes,
                             content_type: str, *, preempt=False, batch_active_fn=None):
    """Binary/multipart inference passthrough (vision classify): relay the raw multipart body + its
    Content-Type to the engine's child unchanged (byte-compat, FR-177). The adapter opts in by
    implementing `forward_multipart(verb, raw_body, content_type, load_ms)`."""
    return _forward_under_lease(
        manager, engine_id, verb,
        lambda ad, load_ms: ad.forward_multipart(verb, raw_body, content_type, load_ms),
        multipart=True, preempt=preempt, batch_active_fn=batch_active_fn)


def stream_frames(manager, rt, engine_id: str, verb: str, body: dict, *, preempt=False,
                  batch_active_fn=None):
    """SSE frame generator for a stream verb — the lock-held-across-generation semantics both
    transports share (T413). Admission/adapter errors raised before the first frame propagate to
    the transport's first `next()`, which maps them via `error_response` and answers JSON (the
    pre-header failure path). A transport closing the generator mid-stream (client disconnect)
    still releases the runtime lock and stamps `last_used`; a pre-header failure does not touch.
    The generator must be driven by ONE thread end-to-end (`rt.lock` is an RLock — thread-affine);
    the ASGI transport dedicates a pump thread per stream for exactly this reason."""
    with rt.lock:  # held across the whole generation (one model in VRAM)
        load_ms = _admit(manager, rt, engine_id, preempt, batch_active_fn)
        gen = rt.adapter.stream(verb, body, load_ms)
        try:
            yield from gen
            rt.touch()
        except GeneratorExit:
            rt.touch()  # client disconnected mid-stream — the child is unaffected (FR-205)
            raise
        finally:
            gen.close()


#: US4 (FR-301) plugs the store schema-compatibility probe in at startup; None = no extra check.
#: Kept a module hook (not a build_agent field) so `/readyz` stays answerable by both the wired
#: agent and the bare test handlers.
_ready_check = None


def _make_store_ready_check(ttl_s: float = 30.0):
    """The /readyz US4 half (FR-299/301): NOT-ready when the gateway DB schema is INCOMPATIBLE
    with this agent (writers refuse rather than evolve). A transient store outage deliberately
    does NOT flip readiness — the supervisor restarts on failed probes, and restarting a healthy
    agent for a Postgres blip fixes nothing (job writes already fail loud on StoreError). The
    verdict is cached for `ttl_s` so a hot-polled /readyz costs one DB round-trip per window."""
    state = {"at": 0.0, "verdict": (True, None)}

    def check():
        now = time.time()
        if now - state["at"] >= ttl_s:
            state["at"] = now
            from platformlib import migrations, store
            try:
                conn = store.connect()
                try:
                    migrations.check_compatible(conn)
                    state["verdict"] = (True, None)
                finally:
                    conn.close()
            except migrations.MigrationError as e:
                state["verdict"] = (False, f"store schema incompatible: {e}")
            except Exception:  # noqa: BLE001 — unreachable store: transient, not a schema verdict
                state["verdict"] = (True, None)
        return state["verdict"]

    return check


def _readiness():
    """(ready, reason) for the public minimal `/readyz` (023 FR-283). Never leaks internals: the
    reason is a short operator hint (e.g. the US4 schema-compatibility verdict), not a stack."""
    if _ready_check is None:
        return True, None
    try:
        return _ready_check()
    except Exception as e:  # noqa: BLE001 — a broken probe reads as not-ready, never a 500
        return False, f"readiness probe failed: {e.__class__.__name__}"


def _refresh_metrics(admission, journal, manager) -> None:
    free = admission.free_gb()
    if free is not None:
        REGISTRY.set_gauge("hostagent_gpu_free_gb", round(free, 2))
    try:  # 023 US7 (FR-322): host disk backing the state dir/models — the LowDiskSpace signal
        import shutil
        REGISTRY.set_gauge("hostagent_disk_free_gb",
                           round(shutil.disk_usage(os.path.expanduser(STATE_DIR)).free / 2**30, 2))
    except OSError:
        pass
    holder = admission.holder()
    REGISTRY.set_gauge("hostagent_slot_held", 1 if holder else 0)
    REGISTRY.set_gauge("hostagent_jobs_active", journal.active_count())
    wedged = 0
    from platformlib.contracts import ENGINE_STATES

    for eid, state in manager.engine_states().items():
        # Emit EVERY state series per engine, current=1 others=0 (Codex round 2, 018): setting
        # only the current one left stale =1 series behind after a transition, so dashboards
        # couldn't tell which state was actually live.
        for name in ENGINE_STATES:
            REGISTRY.set_gauge("hostagent_engine_state", 1 if name == state["state"] else 0,
                               labels={"engine": eid, "state": name})
        if state["state"] == "wedged":
            wedged = 1
    REGISTRY.set_gauge("hostagent_wedged", wedged)


# -- transport-neutral routers (020 T413) ---------------------------------------------------------
# One route table for both runtimes. GET returns (status, payload, content_type) where payload is
# a JSON-able object or pre-rendered bytes; POST returns ("json", status, payload) or
# ("stream", frame_generator) — the transport pulls the FIRST frame before sending headers so a
# pre-stream failure still maps to a JSON error code.

def _legacy_job_get(path: str, jobs):
    """Byte-compat status poll (018 T362, FR-177): the trainer's GET /train/{id} etc. The agent
    renders the JobRecord to the trainer's exact legacy shape; a wrong-kind id 404s just as the
    trainer (a separate dict per kind) did. Returns (status, payload) or None if not routed."""
    segs = path.strip("/").split("/")
    if len(segs) != 2 or segs[0] not in jobs_mod.ROUTE_TO_KIND:
        return None
    rec = jobs.get(segs[1])
    if rec is None or rec.get("kind") != jobs_mod.ROUTE_TO_KIND[segs[0]]:
        return 404, {"error": f"no {segs[0]} {segs[1]}"}
    return 200, jobs_mod.legacy_view(rec)


# -- GET route handlers (024 US3, T585): each `if path == …` branch body, split into a named handler
#    of `(path, ctx)` so it is unit-callable without the HTTP server (tests/test_agent_routes.py). Bodies
#    are byte-preserved; `ctx` is a SimpleNamespace of the deps `handle_get` already received.

def _get_healthz(path, ctx):
    # 023 US2 (FR-283): the PUBLIC liveness probe is minimal — process-up only. The rich
    # operational view (holder/engines/jobs — real state an attacker can enumerate) moved
    # behind the key on `/health`; pre-023 the two paths shared one payload.
    return 200, {"ok": True, "service": "hostagent"}, "application/json"


def _get_readyz(path, ctx):
    # Public minimal readiness (FR-283): can this agent take work? Extended by US4 with the
    # store schema-compatibility verdict (an incompatible schema fails readiness, FR-301).
    ready, reason = _readiness()
    return (200 if ready else 503), {"ready": ready, **({"reason": reason} if reason else {})}, \
        "application/json"


def _get_health(path, ctx):
    admission, manager, journal, jobs, policy = (ctx.admission, ctx.manager, ctx.journal,
                                                 ctx.jobs, ctx.policy)
    holder = admission.holder()
    # The agent's own health PLUS the trainer's `/health` fields (018 T362): TRAINER_URL
    # now points here, so swap (gpu_batch_active), platform_metrics (busy/free_mib/
    # gpu_batch_active), and the gateway training-health view keep reading the same
    # field names with only a base-URL flip (byte-compat, FR-177).
    # Compute engine_states ONCE (019/US7): each call sweeps every engine's readiness
    # probe, and /health is polled hot — computing it twice doubled the child probes.
    states = manager.engine_states()
    # FLAT GPU fields to match platformlib.contracts.AgentHealth (019/US7, FR-195): the
    # prior nested `gpu` object was dropped by the contract's unknown-field filter, so a
    # consumer read the GPU as free/unheld (holder=None) even while a tenant held it.
    return 200, {
        "ok": True,
        "engines": {eid: s["state"] for eid, s in states.items()},
        "gpu_free_gb": admission.free_gb(),
        "holder": (holder or {}).get("tenant"),
        "holder_kind": (holder or {}).get("kind"),
        "wedged": any(s["state"] == "wedged" for s in states.values()),
        "jobs_active": journal.active_count(),
        "interrupted_since_start": _interrupted_at_start,
        "started_at": _started_at,
        # 023 US2 (contracts/agent-security.md §Open-development): the mode is visible on the
        # (now protected) operational health — never any secret material. Additive; contract
        # consumers ignore unknown fields (platformlib.contracts).
        **({"security_mode": policy.security_mode} if policy is not None else {}),
        **jobs.health_fields(),
    }, "application/json"


def _get_metrics(path, ctx):
    _refresh_metrics(ctx.admission, ctx.journal, ctx.manager)
    return 200, REGISTRY.render().encode(), "text/plain; version=0.0.4"


def _get_engines(path, ctx):
    # Contract-shaped rows (019/US7, FR-195): each row carries engine_id/gpu/optional so
    # a consumer can EngineState.from_json(row) — the bare {state:…} value the prior
    # listing returned raised ContractError (validate() requires engine_id).
    return 200, {"engines": ctx.manager.engine_rows()}, "application/json"


def _get_engine_health(path, ctx):
    # Per-engine health/readiness (byte-compat, FR-177): the gateway/swap probe the child-
    # derived engines (vision/embed/tabular) via /readyz|/healthz and the native ones
    # (llm/asr) via /health — the agent answers ALL with the same GPU-lease payload +
    # status, so no gateway probe URL needs to change. gpu_state reads it for the UI/swap.
    manager = ctx.manager
    suffix = next(s for s in ("/health", "/readyz", "/healthz") if path.endswith(s))
    eid = path[len("/engines/"):-len(suffix)]
    rt = manager.runtimes.get(eid)
    if rt is None or not hasattr(rt.adapter, "health"):
        return 404, {"error": f"unknown engine {eid!r}"}, "application/json"
    payload = rt.adapter.health(rt._resident())
    # 503 when the engine can't serve so the gateway readiness aggregator
    # (platform_health keys on status==200), serving.health(), and the supervisor
    # all see a REQUIRED engine as down, not falsely healthy. Two cases (Codex
    # rounds 7/8, 018): the adapter reports NOT ok (missing/dud prereqs), OR the
    # runtime is wedged/disabled — a wedged child (survived SIGKILL, GPU pinned)
    # reports ok:true from the adapter's file checks alone, so fold in the runtime
    # state. A cold/idle engine stays `ok` (available, just not resident).
    st = rt.state().get("state")
    if st in ("wedged", "disabled"):
        payload["ok"] = False
        payload[st] = rt.state().get("reason", True)
    return (200 if payload.get("ok", True) else 503), payload, "application/json"


def _get_jobs(path, ctx):
    journal = ctx.journal
    job_id = path[len("/jobs/"):] if path.startswith("/jobs/") else None
    if job_id:
        rec = journal.get(job_id)
        return (200, rec, "application/json") if rec else \
            (404, {"error": "unknown job"}, "application/json")
    kind = (parse_qs(ctx.query).get("kind") or [None])[0]
    return 200, {"jobs": journal.jobs(kind=kind)}, "application/json"


#: Ordered GET route table (T585): first matcher wins — the exact order of the prior if-ladder.
_GET_ROUTES = (
    (lambda p: p == "/healthz", _get_healthz),
    (lambda p: p == "/readyz", _get_readyz),
    (lambda p: p == "/health", _get_health),
    (lambda p: p == "/metrics", _get_metrics),
    (lambda p: p == "/engines", _get_engines),
    (lambda p: p.startswith("/engines/") and any(
        p.endswith(s) for s in ("/health", "/readyz", "/healthz")), _get_engine_health),
    (lambda p: p == "/jobs" or p.startswith("/jobs/"), _get_jobs),
)


def handle_get(path: str, query: str, *, admission, journal, manager, jobs, policy=None):
    """(status, payload, content_type) for every GET route, dispatched through the ordered
    `_GET_ROUTES` table (024 US3, T586 — first match wins; unmatched falls to the legacy job aliases,
    then 404). Routed on the PARSED path (Codex round 4, 018): the raw request path carries the query
    string, so `/jobs?kind=train` failed the exact match and a stray `/jobsxyz` fell into the jobs
    listing instead of 404. Signature unchanged — callers/tests are byte-compatible (FR-340)."""
    ctx = SimpleNamespace(query=query, admission=admission, journal=journal, manager=manager,
                          jobs=jobs, policy=policy)
    for matcher, handler in _GET_ROUTES:
        if matcher(path):
            return handler(path, ctx)
    legacy = _legacy_job_get(path, jobs)
    if legacy is not None:
        return legacy[0], legacy[1], "application/json"
    return 404, {"error": "unknown path"}, "application/json"


def _parse_json(raw: bytes):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None


# -- POST route handlers (024 US3, T585): each branch body split into a named `(path, ctx)` handler,
#    byte-preserved. `ctx` adds the POST inputs (content_type/control_header/raw_body) to the deps.

def _post_control_unload(path, ctx):
    if CONTROL_SECRET and ctx.control_header != CONTROL_SECRET:
        return "json", 403, {"error": "bad or missing X-Agent-Control"}
    body = _parse_json(ctx.raw_body)
    if body is None:
        return "json", 400, {"error": "invalid JSON"}
    engine = body.get("engine")
    rt = ctx.manager.runtimes.get(engine)
    if rt is None:
        return "json", 404, {"error": f"unknown engine {engine!r}"}
    result = rt.unload(drain_timeout_s=float(body.get("drain_timeout_s", 10)))
    code = 200 if result.get("status") in ("unloaded", "idle") else 409
    return "json", code, result


def _post_control_reload(path, ctx):
    # 022 T466/T467 (FR-255): the gateway's gated promote requests this so the newly selected
    # serving-LLM goes live via a controlled reload — cross-tenant swap or same-tenant
    # force-reload, job holders never preempted (the preserved 409 vocabulary). Secret-gated
    # like /control/unload (state-changing).
    if CONTROL_SECRET and ctx.control_header != CONTROL_SECRET:
        return "json", 403, {"error": "bad or missing X-Agent-Control"}
    body = _parse_json(ctx.raw_body)
    if body is None:
        return "json", 400, {"error": "invalid JSON"}
    manager, jobs = ctx.manager, ctx.jobs
    try:
        # 023 US5 (T523): an operation_id keys the reload for idempotent retry — same op +
        # same target replays the stored result; a different target under the same op is a
        # 409; success requires the EXACT target resident (FR-307/308/312). Absent (a plain
        # 022 caller) this is byte-compatibly the unkeyed reload.
        result = swap_mod.reload_serving_llm_op(
            manager, operation_id=body.get("operation_id"), target=body.get("target"),
            preempt=bool(body.get("preempt")),
            batch_active_fn=lambda: jobs.health_fields()["gpu_batch_active"],
            drain_timeout_s=float(body.get("drain_timeout_s", 10)))
    except swap_mod.TargetUnresolvable as e:
        # FR-265: an unloadable target is distinct from a retryable job-holder/confirm deferral —
        # tag it so the gateway rolls the active-serving-LLM pointer back (a persisted pointer to
        # this target would 503 the next cold load). Still 409 (the preserved vocabulary).
        REGISTRY.inc("hostagent_reload_outcomes_total", labels={"status": "unresolvable"})
        return "json", 409, {"error": str(e), "unresolvable": True}
    except Exception as e:  # noqa: BLE001 — mapped to the preserved status vocabulary
        REGISTRY.inc("hostagent_reload_outcomes_total", labels={"status": "refused"})
        code, payload = error_response(e)
        return "json", code, payload
    # bounded vocabulary (T534/FR-321): loaded|reloaded|swapped|noop|verify_failed
    REGISTRY.inc("hostagent_reload_outcomes_total",
                 labels={"status": result.get("status", "unknown")})
    return "json", 200, result


def _post_jobs(path, ctx):
    body = _parse_json(ctx.raw_body)
    if body is None:
        return "json", 400, {"error": "invalid JSON"}
    # {kind, modality, request}: fold `modality` into the inner request (the trainer
    # carried it there); tolerate a flat body (request fields at top level) too.
    request = dict(body.get("request") or {k: v for k, v in body.items()
                                           if k not in ("kind", "modality", "request")})
    if body.get("modality") is not None:
        request.setdefault("modality", body["modality"])
    try:
        code, payload = ctx.jobs.submit(body.get("kind"), request)
    except StoreError as e:  # US4 T375-B: submit's journal write is a Postgres upsert
        return "json", 502, {"error": str(e)}  # now — a blip is a clean 502, not a
    return "json", code, payload               # dropped connection (@claude PR#46)


def _post_job_cancel(path, ctx):
    if CONTROL_SECRET and ctx.control_header != CONTROL_SECRET:
        return "json", 403, {"error": "bad or missing X-Agent-Control"}
    job_id = path[len("/jobs/"):-len("/cancel")]
    result = ctx.jobs.cancel(job_id)
    code = 404 if result.get("status") == "unknown" else 200
    return "json", code, result


def _post_legacy_alias(path, ctx):
    # Legacy trainer aliases the gateway still calls (byte-compat, FR-177 — retired from the
    # gateway at US4/cleanup).
    body = _parse_json(ctx.raw_body)
    if body is None:
        return "json", 400, {"error": "invalid JSON"}
    try:
        code, payload = ctx.jobs.submit(jobs_mod.ROUTE_TO_KIND[path.strip("/")], body)
    except StoreError as e:  # (@claude PR#46) — same clean-502 mapping for the aliases
        return "json", 502, {"error": str(e)}
    return "json", code, payload


def _post_engine_verb(path, ctx):
    # Inference passthrough: /engines/<id>/<verb> (+ /stream, + `?preempt=true`). The
    # byte-compatible /engines/<id>/unload-now relic retired at T364 (its only caller, the
    # gateway swap, was removed at T363); operators use /control/unload.
    query, content_type, raw_body = ctx.query, ctx.content_type, ctx.raw_body
    manager, jobs = ctx.manager, ctx.jobs
    segs = path.strip("/").split("/")
    eid, verb = segs[1], segs[2]
    # T363: the gateway swap thinned to forwarding `?preempt=true`; the agent
    # orchestrates the evict→admit transactionally (`kind="job"` refuses structurally,
    # and a GPU batch's serving holder refuses via `gpu_batch_active` — FR-155).
    preempt = (parse_qs(query).get("preempt") or [""])[0].lower() in ("1", "true", "yes")
    # A callable (read fresh inside preempt_for at the decision point, not a stale
    # snapshot — @claude PR#37) reporting whether a GPU batch is driving the holder.
    batch_active_fn = (lambda: jobs.health_fields()["gpu_batch_active"]) if preempt \
        else None
    rt = manager.runtimes.get(eid)
    if rt is None:
        return "json", 404, {"error": f"unknown engine {eid!r}"}
    # Strict shape (Codex round 8, 018): exactly /engines/<id>/<verb> or
    # /engines/<id>/<verb>/stream — reject /engines/<id>/<verb>/typo and deeper rather
    # than silently forwarding them as the bare verb.
    stream = len(segs) == 4 and segs[3] == "stream"
    if not (len(segs) == 3 or stream):
        return "json", 404, {"error": "unknown path"}
    # Binary/multipart engines (vision classify) send multipart, not JSON — relay the
    # raw body + Content-Type through to the child unchanged (byte-compat, FR-177).
    if not stream and (content_type or "").startswith("multipart/"):
        code, payload = forward_engine_multipart(
            manager, eid, verb, raw_body, content_type, preempt=preempt,
            batch_active_fn=batch_active_fn)
        return "json", code, payload
    body = _parse_json(raw_body)
    if body is None:
        return "json", 400, {"error": "invalid JSON"}
    if stream:
        if verb not in getattr(rt.adapter, "stream_verbs", ()):
            return "json", 404, {"error": f"engine {eid!r} has no stream verb {verb!r}"}
        return "stream", stream_frames(manager, rt, eid, verb, body, preempt=preempt,
                                       batch_active_fn=batch_active_fn)
    code, payload = forward_engine(manager, eid, verb, body, preempt=preempt,
                                   batch_active_fn=batch_active_fn)
    return "json", code, payload


def _is_engine_verb(path):
    segs = path.strip("/").split("/")
    return len(segs) >= 3 and segs[0] == "engines"


#: Ordered POST route table (T585): first matcher wins — the exact order of the prior if-ladder.
_POST_ROUTES = (
    (lambda p: p == "/control/unload", _post_control_unload),
    (lambda p: p == "/control/reload", _post_control_reload),
    (lambda p: p == "/jobs", _post_jobs),
    (lambda p: p.startswith("/jobs/") and p.endswith("/cancel"), _post_job_cancel),
    (lambda p: p.strip("/") in jobs_mod.ROUTE_TO_KIND, _post_legacy_alias),
    (_is_engine_verb, _post_engine_verb),
)


def handle_post(path: str, query: str, content_type: str, control_header: str, raw_body: bytes,
                *, admission, journal, manager, jobs):
    """Route a POST through the ordered `_POST_ROUTES` table (024 US3, T586 — first match wins;
    unmatched → 404). Returns ("json", status, payload) or ("stream", frame_generator). Signature
    unchanged: the open/keyed/secret-gated semantics + legacy aliases are byte-preserved (FR-340)."""
    ctx = SimpleNamespace(query=query, content_type=content_type, control_header=control_header,
                          raw_body=raw_body, admission=admission, journal=journal, manager=manager,
                          jobs=jobs)
    for matcher, handler in _POST_ROUTES:
        if matcher(path):
            return handler(path, ctx)
    return "json", 404, {"error": "unknown path"}


class BoundedAgentServer(ThreadingHTTPServer):
    """023 US6 (T531, FR-316/318/320): `ThreadingHTTPServer` with deterministic bounds — the raw
    mixin spawns an unbounded thread per connection, so a burst (or a slow-reading client swarm)
    could grow threads/memory without limit on the box that also holds the GPU.

    Two-level admission: at most `max_workers + queue_size` connections are ACCEPTED at once (the
    rest get an immediate minimal 503, before any read); of those, at most `max_workers` execute
    concurrently — the queued remainder wait up to `queue_wait_s` for a worker slot, then 503.
    `graceful_shutdown()` stops accepting, drains in-flight handlers within a budget, and
    best-effort unloads resident engine children (no orphaned llama-server pinning VRAM) — job
    subprocesses are deliberately untouched so the journal's interrupted-marking semantics at next
    start stay exactly as they were (FR-318)."""
    daemon_threads = True

    def __init__(self, addr, handler_cls, *, max_workers=None, queue_size=None,
                 queue_wait_s=None):
        super().__init__(addr, handler_cls)
        workers = max_workers or AGENT_MAX_WORKERS
        self._exec_slots = threading.BoundedSemaphore(workers)
        self._conn_slots = threading.BoundedSemaphore(workers + (queue_size or AGENT_QUEUE_SIZE))
        self._queue_wait_s = AGENT_QUEUE_WAIT_S if queue_wait_s is None else queue_wait_s
        self._inflight = 0
        self._inflight_lock = threading.Lock()

    def process_request(self, request, client_address):
        if not self._conn_slots.acquire(blocking=False):
            REGISTRY.inc("hostagent_requests_rejected_total", labels={"reason": "saturated"})
            self._reject(request)  # bounded queue full: refuse before reading a byte (FR-316)
            return
        super().process_request(request, client_address)  # spawns process_request_thread

    def process_request_thread(self, request, client_address):
        try:
            if not self._exec_slots.acquire(timeout=self._queue_wait_s):
                REGISTRY.inc("hostagent_requests_rejected_total",
                             labels={"reason": "queue_timeout"})
                self._reject(request)
                return
            with self._inflight_lock:
                self._inflight += 1
            try:
                self.finish_request(request, client_address)
            finally:
                with self._inflight_lock:
                    self._inflight -= 1
                self._exec_slots.release()
        except Exception:  # noqa: BLE001 — parity with ThreadingMixIn's containment
            self.handle_error(request, client_address)
        finally:
            self._conn_slots.release()
            self.shutdown_request(request)

    def _reject(self, request):
        body = b'{"error":"agent saturated - bounded worker/queue capacity reached (FR-316)"}'
        try:
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                            b"Content-Type: application/json\r\n"
                            b"Content-Length: " + str(len(body)).encode() +
                            b"\r\nConnection: close\r\n\r\n" + body)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def graceful_shutdown(self, manager=None, timeout_s=None, log=print):
        """Accept-stop → drain → child-cleanup (FR-318). Runs AFTER serve_forever returns."""
        deadline = time.time() + (AGENT_SHUTDOWN_TIMEOUT_S if timeout_s is None else timeout_s)
        while time.time() < deadline:
            with self._inflight_lock:
                if self._inflight == 0:
                    break
            time.sleep(0.05)
        with self._inflight_lock:
            leftover = self._inflight
        if leftover:
            log(f"hostagent shutdown: {leftover} request(s) still in flight after the drain "
                f"budget — closing anyway", flush=True)
        if manager is not None:
            for eid, rt in manager.runtimes.items():
                try:
                    if rt._resident():  # unload children so no orphan pins VRAM after exit
                        rt.unload(drain_timeout_s=2.0)
                except Exception:  # noqa: BLE001 — best-effort; the reaper semantics own the rest
                    pass


def make_handler(admission, journal, manager, jobs, policy=None):
    """The stdlib transport — a thin shell over the shared routers (T413).

    `policy` (023 US2, FR-281/282): the internal-credential gate, checked BEFORE any body read or
    domain call. Defaults to an OPEN policy so bare test handlers keep working; the production
    entry point (`main()`) always wires `auth.from_env()`, which fail-closes without a key."""
    policy = policy or agent_auth.AgentAuthPolicy()

    class Handler(BaseHTTPRequestHandler):
        # Explicit per-socket read/write timeout (023 US6, FR-318): a stalled client can neither
        # park a worker on a half-sent request nor wedge a response write forever. setup() applies
        # it to the connection, so it covers request reads AND response/stream writes.
        timeout = AGENT_IO_TIMEOUT_S

        def log_message(self, *a):  # quiet — health polling would spam stdout
            pass

        def _send(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _deny(self, url) -> bool:
            """True when the request was refused (and answered) by the auth gate — evaluated on
            the PARSED path against the exact public allow-list (FR-283), before any body byte is
            read (FR-282: an unauthorized request produces no admission/journal/store effect)."""
            deny = policy.authorize(self.command, url.path, self.headers.get("X-Agent-Key", ""))
            if deny is None:
                return False
            self._send(*deny)
            return True

        def do_GET(self):
            t0 = time.monotonic()
            REGISTRY.inc("hostagent_requests_total", labels={"method": "GET"})
            try:
                url = urlparse(self.path)
                if self._deny(url):
                    return
                code, payload, ctype = handle_get(url.path, url.query, admission=admission,
                                                  journal=journal, manager=manager, jobs=jobs,
                                                  policy=policy)
                self._send(code, payload, content_type=ctype)
            finally:
                REGISTRY.inc("hostagent_request_seconds_sum", by=time.monotonic() - t0)
                REGISTRY.inc("hostagent_request_seconds_count")

        def _stream(self, gen):
            """SSE response. Pull the first frame before sending headers so a pre-stream failure
            still maps to a JSON error code (the generator holds the runtime lock across the whole
            generation; always close it so the lock releases even on disconnect)."""
            try:
                first = next(gen)  # admission or the adapter's `start` frame can fail here
            except StopIteration:
                first = b""
            except Exception as e:  # noqa: BLE001 — preserved status vocabulary
                gen.close()
                code, payload = error_response(e)
                return self._send(code, payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(first)
                self.wfile.flush()
                for chunk in gen:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                REGISTRY.inc("hostagent_client_disconnects_total")  # mid-stream drop (T534)
            finally:
                gen.close()

        def _body_limit(self) -> int:
            """Per-route-class body cap (023 US6 T532, FR-317): multipart (vision classify, the
            only binary surface) gets the large bound; everything else is JSON-sized."""
            ctype = (self.headers.get("Content-Type") or "").lower()
            return AGENT_MAX_MULTIPART_BYTES if ctype.startswith("multipart/") \
                else AGENT_MAX_JSON_BYTES

        def _read_chunked(self, limit: int):
            """Counted chunked-transfer read (FR-317): aborts at `limit` — an unknown-length body
            cannot buffer unbounded. Accumulates into ONE bytearray so memory tracks the DATA size,
            not the chunk COUNT (review US6-F1: a body dripped as ~1-byte chunks up to the limit
            must not build tens of millions of per-chunk objects → ~1 GB RSS for a 32 MiB payload).
            Returns bytes, or None once the limit is exceeded."""
            total, buf = 0, bytearray()
            while True:
                size_line = self.rfile.readline(64).strip()
                try:
                    size = int(size_line.split(b";")[0] or b"0", 16)
                except ValueError:
                    return None
                if size == 0:
                    self.rfile.readline(4)  # trailing CRLF
                    return bytes(buf)
                total += size
                if total > limit:
                    return None
                buf.extend(self.rfile.read(size))
                self.rfile.readline(4)  # chunk-terminating CRLF

        def _read_body(self):
            """(raw_bytes | None-when-too-large). Declared Content-Length above the limit is
            refused BEFORE any body byte is read; chunked/unknown length is counted as it
            streams (FR-317)."""
            limit = self._body_limit()
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return self._read_chunked(limit)
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > limit:
                return None
            return self.rfile.read(length)

        def do_POST(self):
            t0 = time.monotonic()
            REGISTRY.inc("hostagent_requests_total", labels={"method": "POST"})
            try:
                self._do_post()
            finally:
                REGISTRY.inc("hostagent_request_seconds_sum", by=time.monotonic() - t0)
                REGISTRY.inc("hostagent_request_seconds_count")

        def _do_post(self):
            url = urlparse(self.path)
            if self._deny(url):  # auth BEFORE the body is buffered (FR-282/317: gate, then read)
                return
            raw = self._read_body()
            if raw is None:
                REGISTRY.inc("hostagent_requests_rejected_total", labels={"reason": "too_large"})
                return self._send(413, {"error": f"request body exceeds the "
                                                 f"{self._body_limit()}-byte limit for this "
                                                 f"route class (FR-317)"})
            result = handle_post(url.path, url.query, self.headers.get("Content-Type", ""),
                                 self.headers.get("X-Agent-Control", ""), raw,
                                 admission=admission, journal=journal, manager=manager, jobs=jobs)
            if result[0] == "json":
                _, code, payload = result
                return self._send(code, payload)
            self._stream(result[1])

    return Handler


def main() -> None:
    global _interrupted_at_start, _ready_check
    if os.getenv("AGENT_RUNTIME") not in (None, "", "stdlib"):
        # 023 US6 (T535, FR-315): the dual-runtime drill concluded keep-stdlib
        # (docs/on-hardware-validation.md); the uvicorn transport + AGENT_RUNTIME switch are
        # deleted. Fail loud, not silently-different, for a stale environment.
        raise SystemExit("AGENT_RUNTIME was retired in 023 (stdlib is the one transport) — "
                         "unset it")
    # 023 US2 (FR-284): resolve the credential policy BEFORE building components or binding the
    # socket — a mis-configured agent never comes up half-open. The error names the fix, never a
    # secret value.
    try:
        policy = agent_auth.from_env()
    except agent_auth.AgentAuthConfigError as e:
        raise SystemExit(f"hostagent refused to start: {e}")
    admission, journal, manager, jobs = build_agent()
    _ready_check = _make_store_ready_check()  # /readyz reports schema compatibility (FR-301)
    _interrupted_at_start = journal.mark_interrupted("agent restart")
    if _interrupted_at_start:
        REGISTRY.inc("hostagent_jobs_interrupted_total", by=_interrupted_at_start)
        print(f"!! {_interrupted_at_start} job(s) interrupted by restart — marked failed "
              f"(FR-173)", flush=True)
    threading.Thread(target=manager.run_reaper, daemon=True).start()
    server = BoundedAgentServer((AGENT_BIND, AGENT_PORT),
                                make_handler(admission, journal, manager, jobs, policy=policy))
    print(f"hostagent :{AGENT_PORT} | state={STATE_DIR} "
          f"| engines={list(manager.runtimes)} | jobs=on | vram_budget={VRAM_GB:.0f}GB "
          f"| security={policy.security_mode} "
          f"| workers={AGENT_MAX_WORKERS}+{AGENT_QUEUE_SIZE} io_timeout={AGENT_IO_TIMEOUT_S:.0f}s",
          flush=True)

    # Graceful stop (023 US6 T533, FR-318): SIGTERM/SIGINT → accept-stop, bounded drain, engine
    # children unloaded (no VRAM orphan). Jobs are untouched — still-running jobs are marked
    # interrupted at the NEXT start, the journal semantics that predate this (FR-173).
    import signal

    def _stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()  # never from this thread

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server.serve_forever()
    server.graceful_shutdown(manager)
    print("hostagent: stopped (drained + children unloaded)", flush=True)


if __name__ == "__main__":
    main()
