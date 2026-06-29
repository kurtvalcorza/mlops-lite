"""Native-daemon process supervisor (002 US2, T049/T050 — FR-018).

The GPU/vision daemons run natively in WSL, outside the container engine. 001 started each by
hand (`bash serving/llama/run.sh`, etc.) and they silently died on crash. This pure-stdlib
supervisor starts each, polls their health, and restarts them on exit with exponential
backoff — backing off to a `persistent-unhealthy` state after repeated failures rather than
crash-loop hammering. Daemon states are surfaced over HTTP (`GET /status`) so they're observable.

NOTE: this is distinct from `serving/llama/supervisor.py`, which supervises the *llama-server
model* (load/unload/VRAM). This one supervises the *daemon processes* themselves. It does NOT
touch the one-model-in-VRAM mutex (Principle II) — it only (re)starts processes; the serving↔
training exclusion still lives in those daemons.

Run from WSL:  python3 supervisor/supervise.py
Stop:          Ctrl-C / SIGTERM (terminates all managed children — no GPU orphans).
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POLL_INTERVAL = float(os.getenv("SUPERVISE_POLL_INTERVAL", "3"))
BACKOFF_BASE = float(os.getenv("SUPERVISE_BACKOFF_BASE", "1"))
BACKOFF_CAP = float(os.getenv("SUPERVISE_BACKOFF_CAP", "30"))
MAX_RESTARTS = int(os.getenv("SUPERVISE_MAX_RESTARTS", "5"))   # consecutive failures -> give up
STATUS_PORT = int(os.getenv("SUPERVISE_STATUS_PORT", "8099"))

# Managed daemons: name -> launch command + health probe. Health URLs are localhost because the
# supervisor runs on the same WSL host as the daemons. The set can be narrowed for testing via
# SUPERVISE_DAEMONS (comma-separated names); default is all three.
_ALL = {
    "serving": {
        "cmd": ["bash", os.path.join(REPO, "serving", "llama", "run.sh")],
        "health_url": os.getenv("SERVING_HEALTH", "http://localhost:8090/health"),
        "grace_s": float(os.getenv("SERVING_GRACE", "30")),
    },
    "training": {
        "cmd": ["bash", os.path.join(REPO, "training", "run.sh")],
        "health_url": os.getenv("TRAINING_HEALTH", "http://localhost:8091/health"),
        "grace_s": float(os.getenv("TRAINING_GRACE", "30")),
    },
    "vision": {
        "cmd": ["bash", os.path.join(REPO, "serving", "bento", "run.sh")],
        "health_url": os.getenv("VISION_HEALTH", "http://localhost:8092/readyz"),
        "grace_s": float(os.getenv("VISION_GRACE", "60")),  # bentoml is slow to come up
    },
    # 009 US2 — embeddings: a 5th native CPU daemon, OFF the GPU lease (always-on, no VRAM). BentoML
    # is slow to come up (model download on first load), so the grace is generous like vision.
    "embed": {
        "cmd": ["bash", os.path.join(REPO, "serving", "bento", "embed_run.sh")],
        "health_url": os.getenv("EMBED_HEALTH", "http://localhost:8093/readyz"),
        "grace_s": float(os.getenv("EMBED_GRACE", "120")),
    },
    # 009 US3 — ASR: the whisper.cpp native CUDA supervisor, a GPU-lease tenant (load-on-demand,
    # idle-release VRAM). Like the llama supervisor it loads nothing until the first /transcribe, so a
    # short grace suffices — the supervisor process is up fast even though the model loads lazily.
    "asr": {
        "cmd": ["bash", os.path.join(REPO, "serving", "whispercpp", "run.sh")],
        "health_url": os.getenv("ASR_HEALTH", "http://localhost:8095/health"),
        "grace_s": float(os.getenv("ASR_GRACE", "30")),
    },
    # 009 US4 — tabular: a BentoML CPU daemon, OFF the GPU lease (always-on, no VRAM), like embed.
    "tabular": {
        "cmd": ["bash", os.path.join(REPO, "serving", "bento", "tabular_run.sh")],
        "health_url": os.getenv("TABULAR_HEALTH", "http://localhost:8094/readyz"),
        "grace_s": float(os.getenv("TABULAR_GRACE", "120")),
    },
    # Operator console (003 US1) — a 4th native non-GPU daemon, bound to 127.0.0.1 (FR-025/FR-028).
    # First launch installs deps + builds, so the grace is generous; warm launches `next start` fast.
    "ui": {
        "cmd": ["bash", os.path.join(REPO, "ui", "run.sh")],
        "health_url": os.getenv("UI_HEALTH", "http://localhost:3000/healthz"),
        "grace_s": float(os.getenv("UI_GRACE", "300")),
    },
}
_SELECTED = [n.strip()
             for n in os.getenv("SUPERVISE_DAEMONS",
                                "serving,training,vision,embed,asr,tabular,ui").split(",")
             if n.strip() in _ALL]


def _backoff(failures: int) -> float:
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, failures - 1)))


def _probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


class Daemon:
    """A single supervised native process and its reconciliation state."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.cmd = spec["cmd"]
        self.health_url = spec["health_url"]
        self.grace_s = spec["grace_s"]
        self.proc: subprocess.Popen | None = None
        self.state = "pending"   # pending|starting|healthy|unhealthy|backoff|persistent-unhealthy
        self.restart_count = 0
        self.consecutive_failures = 0
        self.backoff_until = 0.0
        self.started_at = 0.0
        self._exit_registered = False

    def start(self) -> None:
        # Inherit the environment so run.sh sees any exported creds; run.sh also auto-sources .env.
        # start_new_session=True puts the daemon in its own process group so terminate() can signal
        # the WHOLE tree (run.sh → npm → next-server, etc.) — otherwise node grandchildren orphan and
        # keep holding their port, crash-looping every restart with EADDRINUSE.
        self.proc = subprocess.Popen(self.cmd, cwd=REPO, env=os.environ.copy(),
                                     start_new_session=True)
        self.started_at = time.monotonic()
        self.restart_count += 1
        self.state = "starting"
        self._exit_registered = False
        print(f"[supervise] started {self.name} (pid={self.proc.pid}, attempt #{self.restart_count})",
              flush=True)

    def reconcile(self, now: float) -> None:
        alive = self.proc is not None and self.proc.poll() is None
        if not alive:
            if self.proc is None:
                self.start()
                return
            if not self._exit_registered:   # transition alive -> dead: count it once
                self.consecutive_failures += 1
                self.backoff_until = now + _backoff(self.consecutive_failures)
                self._exit_registered = True
                code = self.proc.poll()
                print(f"[supervise] {self.name} exited (code={code}); "
                      f"failure #{self.consecutive_failures}, backoff "
                      f"{self.backoff_until - now:.0f}s", flush=True)
            if self.consecutive_failures > MAX_RESTARTS:
                self.state = "persistent-unhealthy"
                return
            if now < self.backoff_until:
                self.state = "backoff"
                return
            self.start()
            return

        # Process is alive — gate on health.
        if _probe(self.health_url):
            if self.state != "healthy":
                print(f"[supervise] {self.name} healthy", flush=True)
            self.state = "healthy"
            self.consecutive_failures = 0
            self.backoff_until = 0.0
        elif now - self.started_at < self.grace_s:
            self.state = "starting"
        else:
            self.state = "unhealthy"

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            # Signal the whole process group (set up via start_new_session) so grandchildren —
            # node's next-server, npm, the bash wrapper — all die and release their port.
            self._signal_group(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.send_signal(sig)  # fall back to the leader if the group is gone
            except ProcessLookupError:
                pass

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "restart_count": self.restart_count,
            "pid": self.proc.pid if (self.proc and self.proc.poll() is None) else None,
            "health_url": self.health_url,
        }


class Supervisor:
    def __init__(self, names: list[str]):
        self.daemons = [Daemon(n, _ALL[n]) for n in names]
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def monitor_loop(self) -> None:
        while not self.stop.is_set():
            now = time.monotonic()
            with self.lock:
                for d in self.daemons:
                    try:
                        d.reconcile(now)
                    except Exception as e:  # never let one daemon crash the loop
                        print(f"[supervise] reconcile({d.name}) error: {e}", flush=True)
            self.stop.wait(POLL_INTERVAL)

    def snapshot(self) -> dict:
        with self.lock:
            return {"supervisor": "ok", "daemons": [d.snapshot() for d in self.daemons]}

    def shutdown(self, *_):
        print("[supervise] shutting down — terminating daemons (no GPU orphans)", flush=True)
        self.stop.set()
        with self.lock:
            for d in self.daemons:
                d.terminate()


def _make_handler(sup: Supervisor):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/status":
                self._send(200, sup.snapshot())
            elif self.path in ("/health", "/healthz"):
                self._send(200, {"ok": True, "service": "supervisor"})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *_):  # quiet — the monitor logs state transitions instead
            return

    return Handler


def main() -> int:
    if not _SELECTED:
        print("[supervise] no valid daemons selected (SUPERVISE_DAEMONS)", file=sys.stderr)
        return 1
    sup = Supervisor(_SELECTED)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, sup.shutdown)

    monitor = threading.Thread(target=sup.monitor_loop, name="monitor", daemon=True)
    monitor.start()

    server = ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), _make_handler(sup))
    server_thread = threading.Thread(target=server.serve_forever, name="status", daemon=True)
    server_thread.start()
    print(f"[supervise] managing {', '.join(_SELECTED)}; status on :{STATUS_PORT}/status", flush=True)
    try:
        while not sup.stop.is_set():  # signal handler sets stop; Event.wait is interruptible
            sup.stop.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        sup.shutdown()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
