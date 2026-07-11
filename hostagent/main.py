#!/usr/bin/env python3
"""GPU host agent HTTP surface (018 US2, T352 — contracts/agent-api.md; 020 US3, T413).

The single stable endpoint (`platformlib.topology.AGENT_PORT`, :8100). The agent serves the read
surface (health/metrics/engines — open, like the retired per-daemon probes), the control surface
(`POST /control/unload`, opt-in `X-Agent-Control` secret, research R6), engine inference
passthrough (`/engines/<id>/<verb>`, + the byte-compatible legacy paths, FR-177), and the jobs
surface (`/jobs` + the legacy trainer aliases). T364 retired the last legacy daemon and the
lockfile interop shim: admission is the single in-process authority for the one GPU tenant.

020 T413 (FR-205/206): the route table lives in the transport-neutral `handle_get`/`handle_post`
routers below, served by EITHER the stdlib `ThreadingHTTPServer` (default) or uvicorn-ASGI
(`AGENT_RUNTIME=uvicorn`, `hostagent/asgi.py`) — the handler logic does not fork; only the
transport does. `AGENT_BIND`, the control secret, SSE framing, and the 008–017 error vocabulary
are identical on both. The switch exists for the on-hardware runtime drill; the losing runtime is
deleted the following increment (research R7).

Stdlib-only by default (the agent carries no pip deps); uvicorn is imported lazily ONLY when
selected.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from hostagent import admission as adm  # noqa: E402
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


def _refresh_metrics(admission, journal, manager) -> None:
    free = admission.free_gb()
    if free is not None:
        REGISTRY.set_gauge("hostagent_gpu_free_gb", round(free, 2))
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


def handle_get(path: str, query: str, *, admission, journal, manager, jobs):
    """(status, payload, content_type) for every GET route. Routed on the PARSED path (Codex
    round 4, 018): the raw request path carries the query string, so `/jobs?kind=train` failed
    the exact match and a stray `/jobsxyz` fell into the jobs listing instead of 404."""
    if path in ("/health", "/healthz"):
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
            **jobs.health_fields(),
        }, "application/json"
    if path == "/metrics":
        _refresh_metrics(admission, journal, manager)
        return 200, REGISTRY.render().encode(), "text/plain; version=0.0.4"
    if path == "/engines":
        # Contract-shaped rows (019/US7, FR-195): each row carries engine_id/gpu/optional so
        # a consumer can EngineState.from_json(row) — the bare {state:…} value the prior
        # listing returned raised ContractError (validate() requires engine_id).
        return 200, {"engines": manager.engine_rows()}, "application/json"
    if path.startswith("/engines/") and any(
            path.endswith(s) for s in ("/health", "/readyz", "/healthz")):
        # Per-engine health/readiness (byte-compat, FR-177): the gateway/swap probe the child-
        # derived engines (vision/embed/tabular) via /readyz|/healthz and the native ones
        # (llm/asr) via /health — the agent answers ALL with the same GPU-lease payload +
        # status, so no gateway probe URL needs to change. gpu_state reads it for the UI/swap.
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
    if path == "/jobs" or path.startswith("/jobs/"):
        job_id = path[len("/jobs/"):] if path.startswith("/jobs/") else None
        if job_id:
            rec = journal.get(job_id)
            return (200, rec, "application/json") if rec else \
                (404, {"error": "unknown job"}, "application/json")
        kind = (parse_qs(query).get("kind") or [None])[0]
        return 200, {"jobs": journal.jobs(kind=kind)}, "application/json"
    legacy = _legacy_job_get(path, jobs)
    if legacy is not None:
        return legacy[0], legacy[1], "application/json"
    return 404, {"error": "unknown path"}, "application/json"


def _parse_json(raw: bytes):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None


def handle_post(path: str, query: str, content_type: str, control_header: str, raw_body: bytes,
                *, admission, journal, manager, jobs):
    """Route a POST. Returns ("json", status, payload) or ("stream", frame_generator)."""
    if path == "/control/unload":
        if CONTROL_SECRET and control_header != CONTROL_SECRET:
            return "json", 403, {"error": "bad or missing X-Agent-Control"}
        body = _parse_json(raw_body)
        if body is None:
            return "json", 400, {"error": "invalid JSON"}
        engine = body.get("engine")
        rt = manager.runtimes.get(engine)
        if rt is None:
            return "json", 404, {"error": f"unknown engine {engine!r}"}
        result = rt.unload(drain_timeout_s=float(body.get("drain_timeout_s", 10)))
        code = 200 if result.get("status") in ("unloaded", "idle") else 409
        return "json", code, result
    if path == "/control/reload":
        # 022 T466/T467 (FR-255): the gateway's gated promote requests this so the newly selected
        # serving-LLM goes live via a controlled reload — cross-tenant swap or same-tenant
        # force-reload, job holders never preempted (the preserved 409 vocabulary). Secret-gated
        # like /control/unload (state-changing).
        if CONTROL_SECRET and control_header != CONTROL_SECRET:
            return "json", 403, {"error": "bad or missing X-Agent-Control"}
        body = _parse_json(raw_body)
        if body is None:
            return "json", 400, {"error": "invalid JSON"}
        try:
            result = swap_mod.reload_serving_llm(
                manager, preempt=bool(body.get("preempt")),
                batch_active_fn=lambda: jobs.health_fields()["gpu_batch_active"],
                drain_timeout_s=float(body.get("drain_timeout_s", 10)))
        except swap_mod.TargetUnresolvable as e:
            # FR-265: an unloadable target is distinct from a retryable job-holder/confirm deferral —
            # tag it so the gateway rolls the active-serving-LLM pointer back (a persisted pointer to
            # this target would 503 the next cold load). Still 409 (the preserved vocabulary).
            return "json", 409, {"error": str(e), "unresolvable": True}
        except Exception as e:  # noqa: BLE001 — mapped to the preserved status vocabulary
            code, payload = error_response(e)
            return "json", code, payload
        return "json", 200, result
    # Jobs surface (018 T362, contracts/agent-api.md) + the legacy trainer aliases the
    # gateway still calls (byte-compat, FR-177 — retired from the gateway at US4/cleanup).
    if path == "/jobs":
        body = _parse_json(raw_body)
        if body is None:
            return "json", 400, {"error": "invalid JSON"}
        # {kind, modality, request}: fold `modality` into the inner request (the trainer
        # carried it there); tolerate a flat body (request fields at top level) too.
        request = dict(body.get("request") or {k: v for k, v in body.items()
                                               if k not in ("kind", "modality", "request")})
        if body.get("modality") is not None:
            request.setdefault("modality", body["modality"])
        try:
            code, payload = jobs.submit(body.get("kind"), request)
        except StoreError as e:  # US4 T375-B: submit's journal write is a Postgres upsert
            return "json", 502, {"error": str(e)}  # now — a blip is a clean 502, not a
        return "json", code, payload               # dropped connection (@claude PR#46)
    if path.startswith("/jobs/") and path.endswith("/cancel"):
        if CONTROL_SECRET and control_header != CONTROL_SECRET:
            return "json", 403, {"error": "bad or missing X-Agent-Control"}
        job_id = path[len("/jobs/"):-len("/cancel")]
        result = jobs.cancel(job_id)
        code = 404 if result.get("status") == "unknown" else 200
        return "json", code, result
    if path.strip("/") in jobs_mod.ROUTE_TO_KIND:  # legacy trainer aliases
        body = _parse_json(raw_body)
        if body is None:
            return "json", 400, {"error": "invalid JSON"}
        try:
            code, payload = jobs.submit(jobs_mod.ROUTE_TO_KIND[path.strip("/")], body)
        except StoreError as e:  # (@claude PR#46) — same clean-502 mapping for the aliases
            return "json", 502, {"error": str(e)}
        return "json", code, payload
    # Inference passthrough: /engines/<id>/<verb> (+ /stream, + `?preempt=true`). The
    # byte-compatible /engines/<id>/unload-now relic retired at T364 (its only caller, the
    # gateway swap, was removed at T363); operators use /control/unload.
    segs = path.strip("/").split("/")
    if len(segs) >= 3 and segs[0] == "engines":
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
    return "json", 404, {"error": "unknown path"}


def make_handler(admission, journal, manager, jobs):
    """The stdlib transport — a thin shell over the shared routers (T413)."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet — health polling would spam stdout
            pass

        def _send(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            code, payload, ctype = handle_get(url.path, url.query, admission=admission,
                                              journal=journal, manager=manager, jobs=jobs)
            self._send(code, payload, content_type=ctype)

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
                pass  # client disconnected mid-stream
            finally:
                gen.close()

        def do_POST(self):
            url = urlparse(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            result = handle_post(url.path, url.query, self.headers.get("Content-Type", ""),
                                 self.headers.get("X-Agent-Control", ""), raw,
                                 admission=admission, journal=journal, manager=manager, jobs=jobs)
            if result[0] == "json":
                _, code, payload = result
                return self._send(code, payload)
            self._stream(result[1])

    return Handler


def main() -> None:
    global _interrupted_at_start
    runtime = os.getenv("AGENT_RUNTIME", "stdlib")
    if runtime not in ("stdlib", "uvicorn"):
        raise SystemExit(f"AGENT_RUNTIME must be 'stdlib' or 'uvicorn', got {runtime!r}")
    admission, journal, manager, jobs = build_agent()
    _interrupted_at_start = journal.mark_interrupted("agent restart")
    if _interrupted_at_start:
        REGISTRY.inc("hostagent_jobs_interrupted_total", by=_interrupted_at_start)
        print(f"!! {_interrupted_at_start} job(s) interrupted by restart — marked failed "
              f"(FR-173)", flush=True)
    threading.Thread(target=manager.run_reaper, daemon=True).start()
    print(f"hostagent :{AGENT_PORT} | runtime={runtime} | state={STATE_DIR} "
          f"| engines={list(manager.runtimes)} | jobs=on | vram_budget={VRAM_GB:.0f}GB", flush=True)
    if runtime == "uvicorn":
        import uvicorn  # lazy — the stdlib default keeps the agent pip-dep-free (T413)

        from hostagent.asgi import build_asgi_app
        app = build_asgi_app(admission, journal, manager, jobs)
        uvicorn.run(app, host=AGENT_BIND, port=AGENT_PORT, log_level="warning", lifespan="off")
        return
    ThreadingHTTPServer((AGENT_BIND, AGENT_PORT),
                        make_handler(admission, journal, manager, jobs)).serve_forever()


if __name__ == "__main__":
    main()
