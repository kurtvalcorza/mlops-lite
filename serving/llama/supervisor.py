#!/usr/bin/env python3
"""Native llama-server supervisor (Phase 3, hybrid GPU per constitution v1.2.0).

Runs in WSL using only the Python standard library (no pip deps). Owns the llama-server
subprocess lifecycle so the platform honours Principle II — "one model in VRAM, on demand,
released after use":

- loads the model on the GPU on the first request, measuring cold-start time (T017)
- proxies inference to llama-server's OpenAI-compatible endpoint
- unloads the model (frees VRAM) after an idle timeout — not always-on
- refuses to load a model that would exceed the VRAM budget (T016 / FR-004)

Endpoints:
  GET  /health  -> {ok, resident, model, vram_budget_gb, est_vram_gb, fits}
  POST /infer   -> {prompt, max_tokens, temperature}
                   => {text, load_ms, infer_ms, model, usage}
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # serving/ on path
import gpu_lease  # noqa: E402  (shared, stdlib-only GPU lease — 008 US1)

LLAMA_BIN = os.path.expanduser(os.getenv("LLAMA_BIN", "~/llama.cpp/build/bin/llama-server"))
MODEL = os.path.expanduser(os.getenv("MODEL", "~/models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf"))
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "qwen2.5-7b-instruct-q4_k_m")
LLAMA_PORT = int(os.getenv("LLAMA_PORT", "8081"))
SUPERVISOR_PORT = int(os.getenv("SUPERVISOR_PORT", "8090"))
NGL = os.getenv("NGL", "999")
CTX = os.getenv("CTX", "4096")
IDLE_TIMEOUT = float(os.getenv("IDLE_TIMEOUT", "120"))  # seconds idle before VRAM release
VRAM_GB = float(os.getenv("VRAM_GB", "12"))             # from hardware-profile.md
LEASE_TENANT = "llm-serving"                            # this daemon's GPU lease identity (008 US1)

_lock = threading.RLock()
_proc = None
_last_used = 0.0
_last_load_ms = 0.0


def _estimate_vram_gb() -> float:
    """Rough resident VRAM: quantized weights + KV cache / context / overhead headroom."""
    size_gb = os.path.getsize(MODEL) / (1024 ** 3)
    return size_gb * 1.2 + 1.0


def _fits() -> bool:
    return _estimate_vram_gb() <= VRAM_GB * 0.95


def _llama_url(path: str) -> str:
    return f"http://127.0.0.1:{LLAMA_PORT}{path}"


def _server_ready() -> bool:
    try:
        with urllib.request.urlopen(_llama_url("/health"), timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _resident() -> bool:
    return _proc is not None and _proc.poll() is None


def _ensure_loaded() -> float:
    """Start llama-server if needed; return cold-start load_ms (0.0 if already resident).

    008 US1: a cold load first acquires the single race-free GPU lease (FR-062). The lease
    *subsumes* the old `_trainer_busy()` HTTP poll (it refuses atomically while a training run
    holds the slot) and the old `_fits()` static estimate (admission now reads live free VRAM,
    FR-064). Both rejections still surface as RuntimeError → 507, preserving the prior behavior.
    """
    global _proc, _last_load_ms
    if _resident() and _server_ready():
        return 0.0  # already loaded → we already hold the lease; no re-acquire
    try:
        gpu_lease.acquire(LEASE_TENANT, est_gb=_estimate_vram_gb(), vram_budget_gb=VRAM_GB)
    except gpu_lease.LeaseHeld as e:
        holder = (e.holder or {}).get("tenant", "another GPU tenant")
        raise RuntimeError(
            f"GPU busy: {holder} holds the GPU (one model in VRAM, Principle II); "
            f"retry once it releases")
    except gpu_lease.VramExceeded as e:
        raise RuntimeError(str(e))
    # We hold the lease now — start llama-server; release the lease if startup fails (no deadlock).
    try:
        t0 = time.perf_counter()
        _proc = subprocess.Popen(
            [LLAMA_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(LLAMA_PORT),
             "-ngl", NGL, "--ctx-size", CTX, "--alias", MODEL_ALIAS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):  # up to ~60s
            if _server_ready():
                break
            if _proc.poll() is not None:
                raise RuntimeError("llama-server exited during startup")
            time.sleep(0.5)
        else:
            raise RuntimeError("llama-server did not become ready in time")
        # Record the child (the actual VRAM holder) on the lease. The lease's liveness then tracks
        # the llama-server child, not this supervisor — so if the supervisor crashes but the child
        # orphans, the lease stays held (its VRAM is still allocated) and no second tenant can
        # co-reside (Codex P1 #1). (PR_SET_PDEATHSIG was rejected: the cold-load Popen runs in an
        # ephemeral HTTP worker thread, so the parent-death signal would SIGKILL llama-server when
        # that request thread ends — killing the model after one request.)
        gpu_lease.set_vram_owner(LEASE_TENANT, _proc.pid)
        _last_load_ms = round((time.perf_counter() - t0) * 1000, 1)
        return _last_load_ms
    except BaseException:
        _unload()  # kill the half-started proc AND release the lease
        raise


def _unload() -> None:
    global _proc
    if _resident():
        _proc.terminate()
        try:
            _proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _proc.kill()
            # Wait for the kill to land + CUDA VRAM to be freed BEFORE releasing the lease (Codex
            # #10) — else the next tenant could acquire and start allocating while the killed
            # llama-server is still tearing down (transient co-residency).
            try:
                _proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    _proc = None
    gpu_lease.release(LEASE_TENANT)  # free the GPU slot for the next tenant (idle-release / failure)


def _idle_watcher() -> None:
    while True:
        time.sleep(5)
        with _lock:
            if _resident() and _last_used and (time.time() - _last_used) > IDLE_TIMEOUT:
                _unload()


def _do_infer(body: dict) -> dict:
    global _last_used
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 256))
    temperature = float(body.get("temperature", 0.7))
    with _lock:
        load_ms = _ensure_loaded()
        payload = json.dumps({
            "model": MODEL_ALIAS,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()
        req = urllib.request.Request(
            _llama_url("/v1/chat/completions"), data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
        infer_ms = round((time.perf_counter() - t0) * 1000, 1)
        _last_used = time.time()
    return {
        "text": data["choices"][0]["message"]["content"],
        "load_ms": load_ms,
        "infer_ms": infer_ms,
        "model": MODEL_ALIAS,
        "usage": data.get("usage", {}),
    }


def _do_infer_stream(body: dict):
    """Generator of SSE byte-chunks (003 US1): start → token* → done.

    Holds the GPU lock for the whole generation (one model in VRAM, Principle II). llama-server's
    OpenAI-compatible endpoint streams deltas; we re-emit them as small typed SSE events.
    """
    global _last_used
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 256))
    temperature = float(body.get("temperature", 0.7))
    with _lock:
        load_ms = _ensure_loaded()  # may raise RuntimeError (caught before headers are sent)
        payload = json.dumps({
            "model": MODEL_ALIAS,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temperature, "stream": True,
        }).encode()
        req = urllib.request.Request(
            _llama_url("/v1/chat/completions"), data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        yield f"data: {json.dumps({'event': 'start', 'load_ms': load_ms, 'model': MODEL_ALIAS})}\n\n".encode()
        ntok = 0
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                except Exception:
                    delta = None
                if delta:
                    ntok += 1
                    yield f"data: {json.dumps({'event': 'token', 'text': delta})}\n\n".encode()
        infer_ms = round((time.perf_counter() - t0) * 1000, 1)
        _last_used = time.time()
        yield f"data: {json.dumps({'event': 'done', 'infer_ms': infer_ms, 'tokens': ntok, 'model': MODEL_ALIAS})}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *args) -> None:  # keep stdout quiet
        pass

    def do_GET(self) -> None:
        if self.path == "/health":
            # Deliberately NOT under `_lock` (Codex #8): the generation lock is held for the whole
            # backend call, which can outlast the gateway's 5s /health timeout. These are read-only
            # snapshots (a stale bool/lockfile read is fine) — blocking them on a long generation
            # would make /serving/state time out and wrongly report the GPU idle mid-generation.
            holder = gpu_lease.current_holder()  # who holds the single GPU lease (008 FR-068)
            free = gpu_lease.free_vram_gb()       # live free VRAM (admission ground truth, FR-064)
            self._send(200, {
                "ok": True, "resident": _resident(), "model": MODEL_ALIAS,
                "vram_budget_gb": VRAM_GB, "est_vram_gb": round(_estimate_vram_gb(), 1),
                "fits": _fits(),  # kept as a hint only; live VRAM is the gate (FR-064)
                "vram_free_gb": round(free, 1) if free is not None else None,
                "lease_holder": holder.get("tenant") if holder else None,
            })
        elif self.path == "/metrics":
            with _lock:
                body = (
                    "# TYPE serving_resident gauge\n"
                    f"serving_resident {1 if _resident() else 0}\n"
                    "# TYPE serving_vram_budget_gb gauge\n"
                    f"serving_vram_budget_gb {VRAM_GB}\n"
                    "# TYPE serving_est_vram_gb gauge\n"
                    f"serving_est_vram_gb {round(_estimate_vram_gb(), 2)}\n"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/infer", "/infer/stream"):
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"error": "invalid JSON"})
            return
        if self.path == "/infer/stream":
            self._stream(body)
            return
        try:
            self._send(200, _do_infer(body))
        except RuntimeError as e:
            self._send(507, {"error": str(e)})  # model too large / load failure
        except Exception as e:
            self._send(502, {"error": f"inference failed: {e}"})

    def _stream(self, body: dict) -> None:
        """SSE response (003 US1). Pull the first chunk before headers so a cold-load failure
        still maps to a JSON error code; then stream the rest. Always close the generator so the
        GPU lock is released even on client disconnect."""
        gen = _do_infer_stream(body)
        try:
            first = next(gen)
        except RuntimeError as e:
            gen.close()
            self._send(507, {"error": str(e)})
            return
        except Exception as e:
            gen.close()
            self._send(502, {"error": f"inference failed: {e}"})
            return
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
            pass  # client disconnected
        finally:
            gen.close()


def main() -> None:
    print(f"supervisor :{SUPERVISOR_PORT} | model={os.path.basename(MODEL)} "
          f"| est_vram={_estimate_vram_gb():.1f}GB budget={VRAM_GB:.0f}GB fits={_fits()} "
          f"| idle_timeout={IDLE_TIMEOUT}s", flush=True)
    threading.Thread(target=_idle_watcher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", SUPERVISOR_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
