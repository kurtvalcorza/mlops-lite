"""Native training daemon (T031/T032, US4) — runs the LoRA flow on the WSL GPU host.

Mirrors the serving supervisor: a pure-stdlib HTTP server the gateway proxies to. It owns one
training run at a time and enforces the platform's one-model-in-VRAM rule (Principle II) by
refusing to start while the serving model is resident — so training and serving never contend
for the single GPU. The heavy ML stack is imported lazily inside the worker thread, keeping the
daemon light and holding no VRAM while idle.

Endpoints:
  GET  /health            -> {ok, busy, active, gpu_free_mib}
  POST /train             -> {run_id, status}     start a run (background)
  GET  /train/{run_id}    -> {run_id, status, mlflow_run_id, model, metrics, error}

Env: TRAINER_PORT (8091), SUPERVISOR_URL (http://localhost:8090), VRAM_GB (12),
     plus the flow's MLFLOW_TRACKING_URI / MLFLOW_S3_ENDPOINT_URL / AWS_* (default to localhost).
"""
import json
import os
import subprocess
import sys
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # so `flows` is importable

TRAINER_PORT = int(os.getenv("TRAINER_PORT", "8091"))
SUPERVISOR_URL = os.getenv("SUPERVISOR_URL", "http://localhost:8090")
VRAM_GB = float(os.getenv("VRAM_GB", "12"))

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


def _serving_resident() -> bool:
    """True if the serving supervisor currently holds a model in VRAM."""
    try:
        with urllib.request.urlopen(f"{SUPERVISOR_URL}/health", timeout=3) as r:
            return bool(json.load(r).get("resident"))
    except Exception:
        return False  # serving not up → GPU is free for training


def _worker(run_id: str, req: dict):
    global _active
    rec = _runs[run_id]
    try:
        from flows.finetune import finetune_flow  # heavy imports happen here, not at startup
        rec["status"] = "running"
        out = finetune_flow(
            dataset_name=req["dataset_name"],
            dataset_version=req["dataset_version"],
            output_name=req["output_name"],
            base_model=req.get("base_model") or os.getenv("BASE_MODEL", "Qwen/Qwen2.5-0.5B-Instruct"),
            steps=int(req.get("steps", 10)),
            lora_r=int(req.get("lora_r", 8)),
            seed=int(req.get("seed", 0)),
        )
        rec.update(status="completed", mlflow_run_id=out["run_id"],
                   model=out["model"], metrics=out["metrics"], params=out["params"])
    except Exception as e:  # failed run frees the GPU and registers NO partial version (T032)
        rec.update(status="failed", error=f"{type(e).__name__}: {e}")
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    finally:
        with _lock:
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

        with _lock:
            global _active
            if _active is not None:
                return self._send(409, {"error": f"trainer busy with run {_active}"})
            if _serving_resident():
                return self._send(409, {"error": "GPU busy: a model is resident in serving "
                                                 "(one-model-in-VRAM). Let it idle out, then retry."})
            run_id = uuid.uuid4().hex[:12]
            _runs[run_id] = {"run_id": run_id, "status": "queued", "request": req,
                             "mlflow_run_id": None, "model": None, "metrics": None, "error": None}
            _active = run_id
        threading.Thread(target=_worker, args=(run_id, req), daemon=True).start()
        return self._send(202, {"run_id": run_id, "status": "queued"})


def main():
    print(f"trainer :{TRAINER_PORT} | supervisor={SUPERVISOR_URL} | vram_budget={VRAM_GB}GB | "
          f"gpu_free={_gpu_free_mib()}MiB", flush=True)
    ThreadingHTTPServer(("0.0.0.0", TRAINER_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
