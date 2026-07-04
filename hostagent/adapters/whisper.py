"""ASR engine adapter (018 US2, T359) — whisper.cpp, folded in from `serving/whispercpp/supervisor.py`.

The load-on-demand → ready → drain → idle-release → reap lifecycle lives ONCE in
`hostagent.lifecycle`; this adapter supplies only the whisper-specific bits: spawn a `whisper-server`
on a dynamic port, probe readiness (the server has no `/health` — any HTTP response on the base URL
means it is bound), and forward transcription. Stdlib-only.

Byte-compatibility (FR-177): `/engines/asr/transcribe` accepts the SAME JSON the gateway sends the
retired daemon — `{audio_b64, filename?, language?}` → `{text, load_ms, infer_ms, model}` — and the
adapter builds the multipart upload to the whisper-server child by hand (no `requests` dep). Bad/absent
audio raises `ValueError` → the surface's 400, matching the daemon.

OPT-IN engine (`optional=True`, topology.ENGINES): whisper.cpp needs a manual CUDA build
(`serving/whispercpp/build.sh`). When the binary/model is absent `available()` reports unavailable
(the engine is registered but reports NOT ok), so platform-health — which treats asr as optional —
does not stall bring-up. `lease` is read only for the byte-compatible health during migration (T364).
"""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from hostagent.adapters._common import engine_health, free_port

DEFAULT_MODEL = "~/models/whisper/ggml-base.en.bin"
DEFAULT_ALIAS = "whisper-base.en"


class WhisperAdapter:
    engine_id = "asr"
    gpu = True
    optional = True
    verbs = ("transcribe",)   # POST /engines/asr/transcribe
    stream_verbs = ()         # no streaming

    def __init__(self, admission=None):
        self.whisper_bin = os.path.expanduser(
            os.getenv("WHISPER_BIN", "~/whisper.cpp/build/bin/whisper-server"))
        self.model = os.path.expanduser(os.getenv("WHISPER_MODEL", DEFAULT_MODEL))
        self.alias = os.getenv("WHISPER_MODEL_ALIAS", DEFAULT_ALIAS)
        # whisper-server's --convert uses ffmpeg to accept non-WAV uploads (m4a/mp3/webm); the UI
        # accepts audio/*, so default it on. An operator whose build/ffmpeg lacks it can disable.
        self.convert = os.getenv("WHISPER_CONVERT", "1") not in ("", "0", "false", "False")
        self.vram_budget_gb = float(os.getenv("VRAM_GB", "12"))
        self._admission = admission
        self._port = None

    # -- lifecycle interface --------------------------------------------------------------------
    def available(self):
        """Opt-in engine: unavailable (not a crash) when the manual CUDA build / model is absent."""
        if not (os.path.isfile(self.whisper_bin) and os.access(self.whisper_bin, os.X_OK)):
            return (False, f"whisper-server missing or not executable at {self.whisper_bin} — "
                           f"build whisper.cpp (serving/whispercpp/build.sh)")
        if not os.path.isfile(self.model):
            return (False, f"whisper model not found (or not a file) at {self.model}")
        return (True, None)

    def estimate_vram(self) -> float:
        """ggml weights + compute/context overhead (the supervisor's `_estimate_vram_gb`)."""
        try:
            size_gb = os.path.getsize(self.model) / (1024 ** 3)
        except OSError:
            return self.vram_budget_gb * 0.95  # unknown → near-budget, never fail open
        return size_gb * 1.2 + 0.5

    def spawn(self):
        self._port = free_port()
        cmd = [self.whisper_bin, "-m", self.model, "--host", "127.0.0.1", "--port", str(self._port)]
        if self.convert:
            cmd.append("--convert")
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ready(self) -> bool:
        """whisper-server has no /health; any HTTP response on the base URL means it's bound. A
        connection refused (URLError without an HTTP status) means not ready yet."""
        if self._port is None:
            return False
        try:
            with urllib.request.urlopen(self._url("/"), timeout=2) as r:
                return r.status < 500
        except urllib.error.HTTPError:
            return True  # responded (e.g. 404) → up
        except Exception:
            return False

    # -- forward surface ------------------------------------------------------------------------
    def forward(self, verb: str, body: dict, load_ms: float) -> dict:
        """Transcribe: decode the base64 audio and forward it to whisper-server's `/inference` as a
        multipart upload. Byte-compatible response {text, load_ms, infer_ms, model}."""
        if verb != "transcribe":
            raise ValueError(f"asr engine has no verb {verb!r}")
        b64 = body.get("audio_b64", "")
        if not b64:
            raise ValueError("audio_b64 is required")
        try:
            audio = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise ValueError(f"audio_b64 is not valid base64: {e}")
        filename = body.get("filename", "audio.wav")
        language = body.get("language", "auto")
        payload, content_type = _multipart(
            {"temperature": "0.0", "response_format": "json", "language": language},
            "file", filename, audio)
        req = urllib.request.Request(self._url("/inference"), data=payload,
                                     headers={"Content-Type": content_type}, method="POST")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read().decode("utf-8", "ignore")
        infer_ms = round((time.perf_counter() - t0) * 1000, 1)
        try:
            text = json.loads(raw).get("text", "")  # response_format=json
        except Exception:
            text = raw
        return {"text": text.strip(), "load_ms": load_ms, "infer_ms": infer_ms, "model": self.alias}

    def health(self, resident: bool) -> dict:
        ok, reason = self.available()
        est = self.estimate_vram() if os.path.isfile(self.model) else None
        return engine_health(self._admission, ok=ok, reason=reason, resident=resident,
                             model=self.alias, vram_budget_gb=self.vram_budget_gb, est_vram=est)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"


def _hdr_safe(s) -> str:
    """Strip CR/LF and the double-quote that delimits multipart header params (Claude review, 009):
    `filename`/`language` are user-controlled, so a value like `x"\\r\\n--boundary` must not forge a
    header or part. The child is loopback-only, but an embedded quote is a plain correctness bug too."""
    return str(s).replace("\r", "").replace("\n", "").replace('"', "")


def _multipart(fields: dict, file_field: str, filename: str, data: bytes):
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
