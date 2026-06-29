"""Native training daemon (T031/T032, US4; 010 — multimodal dispatch) — runs the fine-tune flows on
the WSL GPU host.

Mirrors the serving supervisor: a pure-stdlib HTTP server the gateway proxies to. It owns one
training run at a time and enforces the platform's one-GPU-tenant rule (Principle II, v1.4.0) by
acquiring the shared race-free GPU lease (008 US1, serving/gpu_lease.py) before a run starts —
atomically refusing while another tenant (serving/vision) holds the slot, and releasing on
completion/failure. This *subsumes* the old refuse-while-serving-resident HTTP poll with the same
409 "GPU busy" semantics, but without its time-of-check/time-of-use race.

Each run executes in a **fresh subprocess** (`run_flow.py`), not in a worker thread: every fine-tune
gets its own CUDA context that the OS tears down on exit, so a crash (e.g. the Whisper/ASR `illegal
memory access` that hit when a long-lived daemon accumulated GPU state across heterogeneous runs)
cannot corrupt the daemon — the next run always starts clean. The daemon itself never imports torch
(holds no VRAM while idle); it records the subprocess as the lease's **vram owner** so the single-GPU
lease tracks the real GPU holder and self-heals on its death.

010: `/train` carries a **`modality` selector** (`llm` | `vision` | `embeddings` | `asr`, default
`llm` for backward compatibility) and one daemon dispatches all four flows behind the **same** single
`_lock` + GPU lease (a fine-tune is the heaviest single tenant — FR-097). No new lock, no new daemon.

Endpoints:
  GET  /health            -> {ok, busy, active, gpu_free_mib}
  POST /train             -> {run_id, status}     start a run (background); body carries `modality`
  GET  /train/{run_id}    -> {run_id, status, mlflow_run_id, model, metrics, error}

Env: TRAINER_PORT (8091), SUPERVISOR_URL (http://localhost:8090), VRAM_GB (12),
     plus the flow's MLFLOW_TRACKING_URI / MLFLOW_S3_ENDPOINT_URL / AWS_* (default to localhost).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so `flow_dispatch` + `flows` are importable
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "serving"))
import gpu_lease  # noqa: E402  (shared, stdlib-only GPU lease — 008 US1)
from flow_dispatch import VALID_MODALITIES  # noqa: E402  (shared with run_flow.py — the subprocess)
from run_flow import read_result  # noqa: E402  (daemon side of the subprocess result protocol)

TRAINER_PORT = int(os.getenv("TRAINER_PORT", "8091"))
SUPERVISOR_URL = os.getenv("SUPERVISOR_URL", "http://localhost:8090")
VRAM_GB = float(os.getenv("VRAM_GB", "12"))
LEASE_TENANT = "training"          # this daemon's GPU lease identity (008 US1)
# Conservative VRAM floor for a LoRA run (base model + adapters + optimizer state). Not an exact
# estimate — just enough that the lease's live-VRAM gate refuses a start when the GPU is already
# mostly consumed (e.g. a leftover CUDA context), instead of letting the worker hit CUDA OOM (#11).
TRAIN_EST_GB = float(os.getenv("TRAIN_EST_GB", "2.0"))
RUN_FLOW = os.path.join(HERE, "run_flow.py")  # the per-run subprocess entry (CUDA isolation)
LOG_DIR = os.getenv("TRAINER_LOG_DIR", os.path.join(tempfile.gettempdir(), "mlops-lite-trainer-logs"))

_lock = threading.Lock()           # one run at a time
_runs: dict = {}                   # run_id -> job record
_active: str | None = None


def _gpu_free_mib():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def _run_subprocess(run_id: str, req: dict) -> dict:
    """Run one fine-tune in a fresh subprocess (`run_flow.py`) and return its job-record update.

    CUDA isolation (the whole point): the flow's torch/CUDA context lives and dies with this child, so
    a crash can't corrupt the long-lived daemon. The request + result travel through temp files (flow
    logs never corrupt the machine-readable result), and the child's stdout/stderr are merged into a
    per-run log we tail on failure. The daemon records the child as the lease's **vram owner** right
    after spawn, so the single-GPU lease's liveness tracks the real VRAM holder: an orphaned child
    keeps the lease held (no co-residency); a dead child frees it.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"run-{run_id}.log")
    with tempfile.TemporaryDirectory(prefix=f"train-{run_id}-") as tmp:
        req_path = os.path.join(tmp, "request.json")
        result_path = os.path.join(tmp, "result.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(req, f)
        # Inherit the daemon env (AWS/MinIO/MLflow + BASE_MODEL/WHISPER_* the flows read).
        with open(log_path, "w", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                [sys.executable, RUN_FLOW, req_path, result_path],
                stdout=logf, stderr=subprocess.STDOUT, env=os.environ.copy())
            # Track the child as the lease's VRAM owner the instant it exists (008.1 vram_pid pattern):
            # the lease now self-heals on the child's death even if this daemon thread is wedged.
            gpu_lease.set_vram_owner(LEASE_TENANT, proc.pid)
            proc.wait()
        # A missing/corrupt result file means the child died without a handled outcome (hard CUDA/OOM
        # crash) — read_result maps both that and {ok:false} to a failed run with the log tail, NO
        # partial version (FR-032/FR-097). Read inside the `with` so result_path still exists.
        return read_result(result_path, proc.returncode, _tail(log_path))


def _tail(path: str, limit: int = 1500) -> str:
    """The last `limit` chars of a file (failure-path log tail) — seek from the end so a large/verbose
    training log isn't loaded into memory whole."""
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read()
    except OSError:
        return ""


def _worker(run_id: str, req: dict):
    global _active
    rec = _runs[run_id]
    try:
        rec["status"] = "running"
        rec.update(_run_subprocess(run_id, req))  # runs in a fresh process — clean CUDA context
    except Exception as e:  # the daemon-side plumbing failed (spawn/IO) — still a failed run, no version
        rec.update(status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        # Release the lease and clear _active **atomically under _lock** (Claude review F1): a
        # concurrent POST /train does its `_active is None` check + `gpu_lease.acquire()` under the
        # same _lock, so it can never observe the in-between state "_active cleared but lease still
        # held" and return a spurious LeaseHeld 409. Releasing inside the lock closes that window.
        # The subprocess (and its CUDA context) is already gone here, so its VRAM is freed by the OS.
        with _lock:
            gpu_lease.release(LEASE_TENANT)  # free the single GPU slot on completion/failure (FR-062)
            _active = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {
                "ok": True, "busy": _active is not None, "active": _active,
                "gpu_free_mib": _gpu_free_mib(), "vram_budget_gb": VRAM_GB,
            })
        if self.path == "/metrics":
            free = _gpu_free_mib()
            lines = [
                "# TYPE trainer_busy gauge",
                f"trainer_busy {1 if _active is not None else 0}",
                "# TYPE trainer_vram_budget_gb gauge",
                f"trainer_vram_budget_gb {VRAM_GB}",
            ]
            if free is not None:
                lines += ["# TYPE trainer_gpu_free_mib gauge", f"trainer_gpu_free_mib {free}"]
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if self.path.startswith("/train/"):
            rid = self.path.split("/train/", 1)[1]
            rec = _runs.get(rid)
            return self._send(200 if rec else 404, rec or {"error": f"no run {rid}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/train":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        for f in ("dataset_name", "dataset_version", "output_name"):
            if not req.get(f):
                return self._send(400, {"error": f"missing field: {f}"})
        # Normalize + validate the modality BEFORE acquiring the lease (010) — an unknown modality is a
        # 400, not a held GPU slot that fails later in the worker.
        modality = (req.get("modality") or "llm").lower()
        if modality not in VALID_MODALITIES:
            return self._send(400, {"error": f"unknown modality {modality!r} "
                                             f"(expected {'|'.join(VALID_MODALITIES)})"})
        req["modality"] = modality

        with _lock:
            global _active
            if _active is not None:
                return self._send(409, {"error": f"trainer busy with run {_active}"})
            # 008 US1: acquire the single race-free GPU lease (FR-062) — this *subsumes* the old
            # `_serving_resident()` HTTP poll, refusing atomically (no TOCTOU window) while another
            # tenant (serving/vision) holds the slot, with the same 409 "GPU busy" semantics. The
            # est_gb floor applies the live-VRAM gate so a mostly-consumed GPU refuses the start
            # (507) instead of the worker hitting CUDA OOM mid-run (Codex #11).
            try:
                gpu_lease.acquire(LEASE_TENANT, est_gb=TRAIN_EST_GB, vram_budget_gb=VRAM_GB)
            except gpu_lease.LeaseHeld as e:
                holder = (e.holder or {}).get("tenant", "another GPU tenant")
                return self._send(409, {"error": f"GPU busy: {holder} holds the GPU "
                                                 "(one-model-in-VRAM). Let it idle out, then retry."})
            except gpu_lease.VramExceeded as e:
                return self._send(507, {"error": str(e)})
            run_id = uuid.uuid4().hex[:12]
            _runs[run_id] = {"run_id": run_id, "status": "queued", "request": req,
                             "mlflow_run_id": None, "model": None, "metrics": None, "error": None}
            _active = run_id
        try:
            threading.Thread(target=_worker, args=(run_id, req), daemon=True).start()
        except BaseException:  # spawn failed — don't strand the lease/slot
            with _lock:  # release + clear atomically, same as the _worker finally (Claude re-review)
                gpu_lease.release(LEASE_TENANT)
                _active = None
            raise
        return self._send(202, {"run_id": run_id, "status": "queued"})


def main():
    print(f"trainer :{TRAINER_PORT} | supervisor={SUPERVISOR_URL} | vram_budget={VRAM_GB}GB | "
          f"gpu_free={_gpu_free_mib()}MiB", flush=True)
    ThreadingHTTPServer(("0.0.0.0", TRAINER_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
