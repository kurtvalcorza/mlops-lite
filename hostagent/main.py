#!/usr/bin/env python3
"""GPU host agent HTTP surface (018 US2, T352 — contracts/agent-api.md).

The single stable endpoint (`platformlib.topology.AGENT_PORT`, :8100 during migration). The
skeleton serves the read surface (health/metrics/engines — open, like today's probes) and the
control surface (`POST /control/unload`, opt-in `X-Agent-Control` secret, research R6). Engine
inference passthrough arrives with each fold-in (T358+); the jobs surface arrives at T362.

Stdlib-only. The engine table starts EMPTY in the skeleton — adapters register per fold-in
phase; until then the legacy daemons keep serving and the agent coexists via the lockfile
interop shim (FR-166).
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
from hostagent import lifecycle  # noqa: E402
from hostagent.journal import Journal  # noqa: E402
from hostagent.metrics import REGISTRY  # noqa: E402
from platformlib.topology import AGENT_PORT, STATE_DIR  # noqa: E402

VRAM_GB = float(os.getenv("VRAM_GB", "12"))
# Codex review (018): the gateway + Prometheus reach the agent via host.docker.internal / an
# injected WSL IP — a loopback-only bind would make AGENT_URL unreachable from the containers the
# moment a fold-in flips traffic here. Default matches the legacy daemons; override to tighten.
AGENT_BIND = os.getenv("AGENT_BIND", "0.0.0.0")
CONTROL_SECRET = os.getenv("AGENT_CONTROL_SECRET") or os.getenv("SWAP_CONTROL_SECRET", "")
# The gateway swap (017) still fronts preemption during migration and calls the holder's
# `unload-now` with X-Swap-Control — so the byte-compatible per-engine `/engines/<id>/unload-now`
# route below honors SWAP_CONTROL_SECRET specifically (not the agent-control secret). Retires when
# the gateway swap thins to the agent's `/control/unload` at T363.
SWAP_CONTROL_SECRET = os.getenv("SWAP_CONTROL_SECRET", "")
JOURNAL_PATH = os.path.join(STATE_DIR, "journal.jsonl")

_started_at = time.time()
_interrupted_at_start = 0


def build_agent(lease=None):
    """Wire the agent's components. `lease` = the legacy lockfile module for migration interop
    (None once the lockfile retires at T364)."""
    if lease is None:
        sys.path.insert(0, os.path.join(_REPO, "serving"))
        import gpu_lease  # noqa: E402 — the interop shim's target (FR-166)

        gpu_lease.verify_shared_state_dir("hostagent")  # 018/FR-166: fail loud on divergence
        lease = gpu_lease
    admission = adm.Admission(vram_budget_gb=VRAM_GB, lease=lease)
    journal = Journal(JOURNAL_PATH)
    from hostagent import adapters  # one runtime per registered engine adapter (T358+)

    manager = lifecycle.EngineManager(admission, runtimes=adapters.build_runtimes(admission, lease))
    return admission, journal, manager


def _engine_error_status(exc: BaseException) -> int:
    """Map a lifecycle/admission failure to the preserved 008–017 error vocabulary."""
    if isinstance(exc, adm.Held):
        return 409
    if isinstance(exc, adm.VramExceeded):
        return 507
    if isinstance(exc, lifecycle.EngineError):
        return 503  # disabled / unavailable / never-ready
    if isinstance(exc, ValueError):
        return 400  # bad body / unknown verb reaching the adapter
    return 502      # child/backend inference failure


def forward_engine(manager, engine_id: str, verb: str, body: dict):
    """Non-streaming inference passthrough (contracts/agent-api.md): resolve the engine, ensure it
    is admitted (cold-load if needed) under the runtime lock, forward to the child, stamp
    `last_used`. Returns (status_code, payload). Framework-free so it is unit-testable without HTTP.
    The runtime lock is held across the whole forward so the reaper/swap cannot unload mid-flight
    (the supervisor's `_lock`-across-generation semantics, now shared)."""
    rt = manager.runtimes.get(engine_id)
    if rt is None:
        return 404, {"error": f"unknown engine {engine_id!r}"}
    if verb not in getattr(rt.adapter, "verbs", ()):
        return 404, {"error": f"engine {engine_id!r} has no verb {verb!r}"}
    try:
        with rt.lock:
            load_ms = rt.ensure_loaded()
            result = rt.adapter.forward(verb, body, load_ms)
            rt.touch()
        return 200, result
    except adm.Held as e:
        return 409, {"error": str(e), "holder": e.holder.get("tenant"),
                     "kind": e.holder.get("kind")}
    except Exception as e:  # noqa: BLE001 — mapped to the preserved status vocabulary
        return _engine_error_status(e), {"error": str(e)}


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


def make_handler(admission, journal, manager):
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
            # Route on the PARSED path (Codex round 4, 018): self.path carries the query string,
            # so `/jobs?kind=train` failed the exact match and a stray `/jobsxyz` fell into the
            # jobs listing instead of 404.
            url = urlparse(self.path)
            path = url.path
            if path in ("/health", "/healthz"):
                holder = admission.holder()
                self._send(200, {
                    "ok": True,
                    "engines": {eid: s["state"] for eid, s in manager.engine_states().items()},
                    "gpu": {"free_gb": admission.free_gb(), "holder": (holder or {}).get("tenant"),
                            "holder_kind": (holder or {}).get("kind"),
                            "wedged": any(s["state"] == "wedged"
                                          for s in manager.engine_states().values())},
                    "jobs_active": journal.active_count(),
                    "interrupted_since_start": _interrupted_at_start,
                    "started_at": _started_at,
                })
            elif path == "/metrics":
                _refresh_metrics(admission, journal, manager)
                self._send(200, REGISTRY.render().encode(),
                           content_type="text/plain; version=0.0.4")
            elif path == "/engines":
                self._send(200, {"engines": manager.engine_states()})
            elif path.startswith("/engines/") and path.endswith("/health"):
                # Per-engine health (byte-compatible with the retired daemon's /health, FR-177): the
                # gateway's serving.gpu_state reads it for the UI status line + swap holder.
                eid = path[len("/engines/"):-len("/health")]
                rt = manager.runtimes.get(eid)
                if rt is None or not hasattr(rt.adapter, "health"):
                    self._send(404, {"error": f"unknown engine {eid!r}"})
                else:
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
                    self._send(200 if payload.get("ok", True) else 503, payload)
            elif path == "/jobs" or path.startswith("/jobs/"):
                job_id = path[len("/jobs/"):] if path.startswith("/jobs/") else None
                if job_id:
                    rec = journal.get(job_id)
                    self._send(200, rec) if rec else self._send(404, {"error": "unknown job"})
                else:
                    kind = (parse_qs(url.query).get("kind") or [None])[0]
                    self._send(200, {"jobs": journal.jobs(kind=kind)})
            else:
                self._send(404, {"error": "unknown path"})

        def _read_body(self):
            try:
                return json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            except ValueError:
                return None

        def _stream(self, gen):
            """SSE response. Pull the first frame before sending headers so a pre-stream failure
            still maps to a JSON error code (the caller holds the runtime lock across the whole
            generation; always close the generator so the lock releases even on disconnect)."""
            first = next(gen)  # the adapter's `start` frame — a backend error surfaces mid-stream
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
            path = urlparse(self.path).path
            if path == "/control/unload":
                if CONTROL_SECRET and self.headers.get("X-Agent-Control", "") != CONTROL_SECRET:
                    return self._send(403, {"error": "bad or missing X-Agent-Control"})
                body = self._read_body()
                if body is None:
                    return self._send(400, {"error": "invalid JSON"})
                engine = body.get("engine")
                rt = manager.runtimes.get(engine)
                if rt is None:
                    return self._send(404, {"error": f"unknown engine {engine!r}"})
                result = rt.unload(drain_timeout_s=float(body.get("drain_timeout_s", 10)))
                code = 200 if result.get("status") in ("unloaded", "idle") else 409
                return self._send(code, result)
            # Inference passthrough: /engines/<id>/<verb> (+ /stream), and the migration-only
            # byte-compatible /engines/<id>/unload-now the gateway swap still calls (retires T363).
            segs = path.strip("/").split("/")
            if len(segs) >= 3 and segs[0] == "engines":
                eid, verb = segs[1], segs[2]
                rt = manager.runtimes.get(eid)
                if rt is None:
                    return self._send(404, {"error": f"unknown engine {eid!r}"})
                # Strict shape (Codex round 8, 018): exactly /engines/<id>/<verb> or
                # /engines/<id>/<verb>/stream — reject /engines/<id>/<verb>/typo and deeper rather
                # than silently forwarding them as the bare verb.
                stream = len(segs) == 4 and segs[3] == "stream"
                if not (len(segs) == 3 or stream):
                    return self._send(404, {"error": "unknown path"})
                if verb == "unload-now" and len(segs) == 3:
                    if SWAP_CONTROL_SECRET and \
                            self.headers.get("X-Swap-Control", "") != SWAP_CONTROL_SECRET:
                        return self._send(401, {"error": "unauthorized unload-now "
                                                         "(bad or missing X-Swap-Control)"})
                    body = self._read_body()
                    if body is None:
                        return self._send(400, {"error": "invalid JSON"})
                    result = rt.unload(drain_timeout_s=float(body.get("drain_timeout_s", 10)))
                    # Byte-compat with the retired supervisor's /unload-now (Codex round 7, 018): it
                    # returned exactly {"status": "idle"} for the not-resident/stale-snapshot no-op,
                    # without the shared lifecycle's extra `drained` key. Gateway swap reads only
                    # `status`, but this route is the advertised byte-compatible endpoint.
                    if result.get("status") == "idle":
                        result = {"status": "idle"}
                    code = 200 if result.get("status") in ("unloaded", "idle") else 409
                    return self._send(code, result)
                body = self._read_body()
                if body is None:
                    return self._send(400, {"error": "invalid JSON"})
                if stream:
                    if verb not in getattr(rt.adapter, "stream_verbs", ()):
                        return self._send(404, {"error": f"engine {eid!r} has no stream "
                                                         f"verb {verb!r}"})
                    try:
                        with rt.lock:  # held across the whole generation (one model in VRAM)
                            load_ms = rt.ensure_loaded()
                            gen = rt.adapter.stream(verb, body, load_ms)
                            try:
                                self._stream(gen)
                                rt.touch()
                            except Exception as e:  # pre-header failure (bad verb/backend connect)
                                gen.close()
                                self._send(_engine_error_status(e), {"error": str(e)})
                    except adm.Held as e:
                        self._send(409, {"error": str(e), "holder": e.holder.get("tenant"),
                                         "kind": e.holder.get("kind")})
                    except Exception as e:  # noqa: BLE001 — preserved status vocabulary
                        self._send(_engine_error_status(e), {"error": str(e)})
                    return
                code, payload = forward_engine(manager, eid, verb, body)
                return self._send(code, payload)
            self._send(404, {"error": "unknown path"})

    return Handler


def main() -> None:
    global _interrupted_at_start
    admission, journal, manager = build_agent()
    _interrupted_at_start = journal.mark_interrupted("agent restart")
    if _interrupted_at_start:
        REGISTRY.inc("hostagent_jobs_interrupted_total", by=_interrupted_at_start)
        print(f"!! {_interrupted_at_start} job(s) interrupted by restart — marked failed "
              f"(FR-173)", flush=True)
    threading.Thread(target=manager.run_reaper, daemon=True).start()
    print(f"hostagent :{AGENT_PORT} | state={STATE_DIR} | engines={list(manager.runtimes)} "
          f"| vram_budget={VRAM_GB:.0f}GB", flush=True)
    ThreadingHTTPServer((AGENT_BIND, AGENT_PORT), make_handler(admission, journal, manager)) \
        .serve_forever()


if __name__ == "__main__":
    main()
