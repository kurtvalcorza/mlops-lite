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

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from platformlib.topology import AGENT_PORT, STATE_DIR  # noqa: E402

from hostagent import admission as adm  # noqa: E402
from hostagent import lifecycle, swap  # noqa: E402
from hostagent.journal import Journal  # noqa: E402
from hostagent.metrics import REGISTRY  # noqa: E402

VRAM_GB = float(os.getenv("VRAM_GB", "12"))
# Codex review (018): the gateway + Prometheus reach the agent via host.docker.internal / an
# injected WSL IP — a loopback-only bind would make AGENT_URL unreachable from the containers the
# moment a fold-in flips traffic here. Default matches the legacy daemons; override to tighten.
AGENT_BIND = os.getenv("AGENT_BIND", "0.0.0.0")
CONTROL_SECRET = os.getenv("AGENT_CONTROL_SECRET") or os.getenv("SWAP_CONTROL_SECRET", "")
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
    manager = lifecycle.EngineManager(admission, runtimes={})  # adapters register at T358+
    return admission, journal, manager


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
            if self.path in ("/health", "/healthz"):
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
            elif self.path == "/metrics":
                _refresh_metrics(admission, journal, manager)
                self._send(200, REGISTRY.render().encode(), content_type="text/plain; version=0.0.4")
            elif self.path == "/engines":
                self._send(200, {"engines": manager.engine_states()})
            elif self.path.startswith("/jobs"):
                job_id = self.path[len("/jobs/"):] if self.path.startswith("/jobs/") else None
                if job_id:
                    rec = journal.get(job_id)
                    self._send(200, rec) if rec else self._send(404, {"error": "unknown job"})
                else:
                    self._send(200, {"jobs": journal.jobs()})
            else:
                self._send(404, {"error": "unknown path"})

        def do_POST(self):
            if self.path == "/control/unload":
                if CONTROL_SECRET and self.headers.get("X-Agent-Control", "") != CONTROL_SECRET:
                    return self._send(403, {"error": "bad or missing X-Agent-Control"})
                try:
                    body = json.loads(self.rfile.read(
                        int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                engine = body.get("engine")
                rt = manager.runtimes.get(engine)
                if rt is None:
                    return self._send(404, {"error": f"unknown engine {engine!r}"})
                result = rt.unload(drain_timeout_s=float(body.get("drain_timeout_s", 10)))
                code = 200 if result.get("status") in ("unloaded", "idle") else 409
                return self._send(code, result)
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
