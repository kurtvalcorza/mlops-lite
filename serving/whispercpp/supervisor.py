#!/usr/bin/env python3
"""Native whisper.cpp ASR supervisor (009 US3, T166 — FR-079; hybrid GPU v1.2.0).

A NEW GPU-lease tenant added in 009, mirroring serving/llama/supervisor.py EXACTLY so it inherits the
008.1 lease correctness rather than re-deriving it:

- loads the whisper-server child on the GPU on the first request (cold-start measured),
- proxies transcription to whisper-server's `/inference` endpoint,
- **joins the single race-free GPU lease** (serving/gpu_lease.py) as tenant ``asr`` — records the
  whisper-server child as the VRAM owner (vram_pid) so it is never co-resident with the LLM/vision/a
  training run (Principle II, one GPU tenant at a time),
- unloads (frees VRAM + releases the lease) after an idle timeout — not always-on,
- refuses to load when live free VRAM can't admit the model (→ 507), like the LLM supervisor.

Stdlib-only (no pip deps), so it runs in the bare WSL host like the llama supervisor. Whisper needs
binary audio, so /transcribe accepts JSON ``{audio_b64, filename?}`` (what the gateway proxies) and
forwards it to whisper-server as a multipart upload built by hand (no requests dep).

Endpoints:
  GET  /health      -> {ok, resident, model, vram_budget_gb, vram_free_gb, lease_holder}
  POST /transcribe  -> {audio_b64, filename?, language?} => {text, load_ms, infer_ms, model}
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # serving/ on path
import gpu_lease  # noqa: E402  (shared, stdlib-only GPU lease — 008/009 US3)

WHISPER_BIN = os.path.expanduser(os.getenv("WHISPER_BIN", "~/whisper.cpp/build/bin/whisper-server"))
MODEL = os.path.expanduser(os.getenv("WHISPER_MODEL", "~/models/whisper/ggml-base.en.bin"))
MODEL_ALIAS = os.getenv("WHISPER_MODEL_ALIAS", "whisper-base.en")
WHISPER_PORT = int(os.getenv("WHISPER_PORT", "8082"))
ASR_PORT = int(os.getenv("ASR_PORT", "8095"))
IDLE_TIMEOUT = float(os.getenv("ASR_IDLE_TIMEOUT", "120"))  # seconds idle before VRAM release
VRAM_GB = float(os.getenv("VRAM_GB", "12"))                 # from hardware-profile.md
LEASE_TENANT = "asr"                                        # this daemon's GPU lease identity (009 US3)
# whisper-server's --convert uses ffmpeg to accept non-WAV uploads (m4a/mp3/webm). The UI accepts
# audio/*, so default it on; an operator whose whisper build/ffmpeg lacks it can disable (Codex review).
WHISPER_CONVERT = os.getenv("WHISPER_CONVERT", "1") not in ("", "0", "false", "False")

_lock = threading.RLock()
_proc = None
_last_used = 0.0
_last_load_ms = 0.0


class GpuBusy(Exception):
    """Another tenant holds the single GPU lease (FR-083) — surfaced as 409, like vision's busy marker
    (008), so a lease-governed transcribe is refused cleanly (the UI gates it on the lease)."""


def _estimate_vram_gb() -> float:
    """Rough resident VRAM for a whisper model: ggml weights + compute/context overhead headroom."""
    try:
        size_gb = os.path.getsize(MODEL) / (1024 ** 3)
    except OSError:
        size_gb = 1.0  # model missing → a conservative non-zero so admission still gates
    return size_gb * 1.2 + 0.5


def _fits() -> bool:
    return _estimate_vram_gb() <= VRAM_GB * 0.95


def _whisper_url(path: str) -> str:
    return f"http://127.0.0.1:{WHISPER_PORT}{path}"


def _server_ready() -> bool:
    """whisper-server has no /health; any HTTP response on the base URL means it's bound + serving.
    A connection refused (URLError without an HTTP status) means not ready yet."""
    try:
        with urllib.request.urlopen(_whisper_url("/"), timeout=2) as r:
            return r.status < 500
    except urllib.error.HTTPError:
        return True  # responded (e.g. 404) → the server is up
    except Exception:
        return False


def _resident() -> bool:
    p = _proc  # snapshot — /health reads this without _lock, so don't re-read the global mid-check
    return p is not None and p.poll() is None


def _ensure_loaded() -> float:
    """Start whisper-server if needed; return cold-start load_ms (0.0 if already resident).

    Mirrors the llama supervisor (008.1): acquire the single GPU lease first (refuses atomically while
    another tenant holds it; live-VRAM admission), record the whisper-server child as the lease's
    vram_pid right after Popen, then wait for readiness. Releases the lease if startup fails.
    """
    global _proc, _last_load_ms
    if _resident() and _server_ready():
        return 0.0  # already loaded → we already hold the lease; no re-acquire
    # Resident but NOT ready (a stuck/unresponsive child) → reap it BEFORE relaunching (Codex review).
    # Otherwise we'd Popen a second child, overwrite _proc, and orphan the first — which still holds
    # VRAM + the port; when the second (failed) child exits, _unload() would release the lease while
    # the orphan still occupies the GPU, breaking the single-tenant invariant.
    if _resident():
        _unload()  # kill the stuck child + release the lease; we re-acquire fresh below
    try:
        gpu_lease.acquire(LEASE_TENANT, est_gb=_estimate_vram_gb(), vram_budget_gb=VRAM_GB)
    except gpu_lease.LeaseHeld as e:
        holder = (e.holder or {}).get("tenant", "another GPU tenant")
        raise GpuBusy(
            f"GPU busy: {holder} holds the GPU (one model in VRAM, Principle II); "
            f"free it (idle-release or stop serving) to transcribe")
    except gpu_lease.VramExceeded as e:
        raise RuntimeError(str(e))
    try:
        t0 = time.perf_counter()
        cmd = [WHISPER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(WHISPER_PORT)]
        if WHISPER_CONVERT:
            cmd.append("--convert")  # accept non-WAV uploads via ffmpeg (the UI accepts audio/*)
        _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Record the child (the actual VRAM holder) on the lease IMMEDIATELY — same reasoning as the
        # llama supervisor (Codex 008.1 P1 #1): the lease's liveness then tracks the whisper-server
        # child, not this supervisor, so an orphaned child keeps the lease held (no co-residency) and
        # a dead child frees it. PR_SET_PDEATHSIG was rejected there for the same thread-lifetime
        # reason and is intentionally NOT used here.
        gpu_lease.set_vram_owner(LEASE_TENANT, _proc.pid)
        for _ in range(120):  # up to ~60s
            if _server_ready():
                break
            if _proc.poll() is not None:
                raise RuntimeError("whisper-server exited during startup")
            time.sleep(0.5)
        else:
            raise RuntimeError("whisper-server did not become ready in time")
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
            try:
                _proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    # Only release the lease once the child is actually gone (008.1 PR#5 P2): if a SIGKILLed child is
    # somehow still alive (uninterruptible D-state), KEEP the lease — its vram_pid keeps it live so no
    # tenant co-resides — and leave _proc set so a later idle/unload pass retries the reap.
    if _proc is not None and _proc.poll() is None:
        return
    _proc = None
    gpu_lease.release(LEASE_TENANT)  # free the GPU slot for the next tenant (idle-release / failure)


def _unload_now(drain_timeout_s: float) -> dict:
    """017 (FR-156): drain in-flight transcription within a bounded timeout, then unload + release the GPU
    lease — the gateway's `unload-now` control call during a swap. The GPU `_lock` is held for the whole
    duration of an in-flight `_do_transcribe`, so acquiring it === drained (no new work can start either);
    on timeout we hard-unload so a stuck request can't hang the swap. Idempotent: not resident → `idle`."""
    if not _resident():
        return {"status": "idle"}
    acquired = _lock.acquire(timeout=max(drain_timeout_s, 0.0))
    try:
        if not _resident():
            return {"status": "idle"}
        _unload()
        return {"status": "unloaded", "drained": acquired}
    finally:
        if acquired:
            _lock.release()


def _idle_watcher() -> None:
    while True:
        time.sleep(5)
        with _lock:
            if _resident() and _last_used and (time.time() - _last_used) > IDLE_TIMEOUT:
                _unload()


def _hdr_safe(s) -> str:
    """Strip characters that could break out of a multipart header / inject a part — CR, LF, and the
    double-quote that delimits header params (Claude review). `filename` and `language` are
    user-controlled, so a value like `x"\r\n--boundary` must not be able to forge a header or part.
    whisper-server is loopback-only, but a filename with an embedded quote is also a plain correctness
    bug, so every field value + the filename is sanitized here (defense at the point of assembly)."""
    return str(s).replace("\r", "").replace("\n", "").replace('"', "")


def _multipart(fields: dict, file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body (stdlib only) for the whisper-server upload."""
    boundary = f"----mlopslite{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts = []
    for k, v in fields.items():
        parts.append(b"--" + boundary.encode() + crlf)
        parts.append(f'Content-Disposition: form-data; name="{_hdr_safe(k)}"'.encode() + crlf + crlf)
        parts.append(_hdr_safe(v).encode() + crlf)
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(
        f'Content-Disposition: form-data; name="{_hdr_safe(file_field)}"; '
        f'filename="{_hdr_safe(filename)}"'.encode() + crlf)
    parts.append(b"Content-Type: application/octet-stream" + crlf + crlf)
    parts.append(data + crlf)
    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _do_transcribe(body: dict) -> dict:
    global _last_used
    b64 = body.get("audio_b64", "")
    if not b64:
        raise ValueError("audio_b64 is required")
    try:
        audio = base64.b64decode(b64, validate=True)
    except Exception as e:
        raise ValueError(f"audio_b64 is not valid base64: {e}")
    filename = body.get("filename", "audio.wav")
    language = body.get("language", "auto")
    with _lock:
        load_ms = _ensure_loaded()
        # Stamp _last_used right after a successful (cold) load — BEFORE the backend call (Codex
        # review): if inference then fails (e.g. corrupt audio), the idle watcher still reaps the
        # whisper-server child and releases the lease, instead of holding VRAM forever after a failed
        # first transcribe (its guard requires _last_used to be truthy).
        _last_used = time.time()
        payload, content_type = _multipart(
            {"temperature": "0.0", "response_format": "json", "language": language},
            "file", filename, audio)
        req = urllib.request.Request(
            _whisper_url("/inference"), data=payload,
            headers={"Content-Type": content_type}, method="POST")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read().decode("utf-8", "ignore")
        infer_ms = round((time.perf_counter() - t0) * 1000, 1)
        _last_used = time.time()
    # whisper-server returns {"text": "..."} for response_format=json; tolerate a plain-text body.
    try:
        text = json.loads(raw).get("text", "")
    except Exception:
        text = raw
    return {"text": text.strip(), "load_ms": load_ms, "infer_ms": infer_ms, "model": MODEL_ALIAS}


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
            # Read the lease OUTSIDE _lock (008.1 #8): a long transcription holds the generation lock,
            # which can outlast the gateway's 5s /health timeout; these are read-only snapshots.
            holder = gpu_lease.current_holder()
            free = gpu_lease.free_vram_gb()
            self._send(200, {
                "ok": True, "resident": _resident(), "model": MODEL_ALIAS,
                "vram_budget_gb": VRAM_GB, "est_vram_gb": round(_estimate_vram_gb(), 1),
                "fits": _fits(),
                "vram_free_gb": round(free, 1) if free is not None else None,
                "lease_holder": holder.get("tenant") if holder else None,
            })
        elif self.path == "/metrics":
            with _lock:
                body = (
                    "# TYPE asr_resident gauge\n"
                    f"asr_resident {1 if _resident() else 0}\n"
                    "# TYPE asr_vram_budget_gb gauge\n"
                    f"asr_vram_budget_gb {VRAM_GB}\n"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/transcribe", "/unload-now"):
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"error": "invalid JSON"})
            return
        if self.path == "/unload-now":
            # 017: gateway-only control call during a swap. Gateway-gated on the private WSL network (like
            # /transcribe). Opt-in defense-in-depth for this *destructive* route: when SWAP_CONTROL_SECRET
            # is set, require the X-Swap-Control header the gateway forwards; unset → 008 behavior.
            secret = os.getenv("SWAP_CONTROL_SECRET", "")
            if secret and self.headers.get("X-Swap-Control", "") != secret:
                self._send(401, {"error": "unauthorized unload-now (bad or missing X-Swap-Control)"})
                return
            try:
                self._send(200, _unload_now(float(body.get("drain_timeout_s", 10))))
            except Exception as e:
                self._send(500, {"error": f"unload-now failed: {e}"})
            return
        try:
            self._send(200, _do_transcribe(body))
        except ValueError as e:
            self._send(400, {"error": str(e)})           # bad/missing audio
        except GpuBusy as e:
            self._send(409, {"error": str(e)})           # lease held by another GPU tenant
        except RuntimeError as e:
            self._send(507, {"error": str(e)})           # VRAM admission / load failure
        except Exception as e:
            self._send(502, {"error": f"transcription failed: {e}"})


def main() -> None:
    gpu_lease.verify_shared_state_dir("whisper-supervisor")  # 018/FR-166: fail loud on divergence
    print(f"asr supervisor :{ASR_PORT} | model={os.path.basename(MODEL)} "
          f"| est_vram={_estimate_vram_gb():.1f}GB budget={VRAM_GB:.0f}GB fits={_fits()} "
          f"| idle_timeout={IDLE_TIMEOUT}s", flush=True)
    threading.Thread(target=_idle_watcher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", ASR_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
