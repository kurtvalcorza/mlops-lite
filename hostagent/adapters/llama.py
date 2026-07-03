"""LLM engine adapter (018 US2, T358) — llama.cpp, folded in from `serving/llama/supervisor.py`.

The load-on-demand → ready → drain → idle-release → reap lifecycle now lives ONCE in
`hostagent.lifecycle`; this adapter supplies only the llama-specific bits: spawn a `llama-server`
on a **dynamic** port (no fixed :8081 to collide with a peer engine's child), probe its readiness,
and forward inference — REST and SSE — to it. Stdlib-only (the agent carries no pip deps).

Byte-compatibility (FR-177): the forward request/response bodies and the SSE frames are identical to
the retired supervisor's, so the gateway's serving router + `/infer/stream` proxy change base URL
only. The SSE `done` frame in particular must keep the exact `"event": "done"` marker the gateway's
stream proxy scans for.

Migration note (T358→T364): `lease` is the legacy lockfile module, read ONLY to build the
byte-compatible `/engines/llm/health` GLOBAL holder + free-VRAM fields. During the strangler
migration the vision/asr/training legacy daemons still hold the same lockfile, so the agent's
in-process admission holder is not the whole truth — the lockfile is. At lockfile retirement (T364)
the health source flips to `admission.holder()` and the `lease` argument goes away.
"""
import json
import os
import socket
import subprocess
import time
import urllib.request

DEFAULT_MODEL = "~/models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
DEFAULT_ALIAS = "qwen2.5-7b-instruct-q4_k_m"


def _free_port() -> int:
    """An ephemeral loopback port for the llama-server child. Small bind→close→respawn TOCTOU
    window (same host, ephemeral range) — acceptable, and it removes the fixed-port EADDRINUSE
    class of failure the old supervisor's :8081 had against an orphaned child."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaAdapter:
    """Adapter for the text-generation (llm) engine. Implements the duck-typed
    `data-model.md §EngineAdapter` interface plus the engine-specific forward surface the agent
    routes to (`verbs` / `stream_verbs` / `forward` / `stream` / `health`)."""

    engine_id = "llm"
    gpu = True
    optional = False
    verbs = ("infer",)         # POST /engines/llm/infer
    stream_verbs = ("infer",)  # POST /engines/llm/infer/stream (SSE)

    def __init__(self, lease=None):
        self.llama_bin = os.path.expanduser(
            os.getenv("LLAMA_BIN", "~/llama.cpp/build/bin/llama-server"))
        self.model = os.path.expanduser(os.getenv("MODEL", DEFAULT_MODEL))
        self.alias = os.getenv("MODEL_ALIAS", DEFAULT_ALIAS)
        self.ngl = os.getenv("NGL", "999")
        self.ctx = os.getenv("CTX", "4096")
        self.vram_budget_gb = float(os.getenv("VRAM_GB", "12"))
        self._lease = lease
        self._port = None  # set on spawn(); the child's dynamic port

    # -- lifecycle interface (data-model.md §EngineAdapter) --------------------------------------
    def available(self):
        """Binary + model prerequisites present? Surfaces as `unavailable(reason)` (R7) instead of
        a spawn crash — the old supervisor discovered a missing binary only at load time."""
        if not os.path.exists(self.llama_bin):
            return (False, f"llama-server not found at {self.llama_bin} — build llama.cpp (README)")
        if not os.path.exists(self.model):
            return (False, f"model GGUF not found at {self.model}")
        return (True, None)

    def estimate_vram(self) -> float:
        """Quantized weights + KV/context/overhead headroom (the supervisor's `_estimate_vram_gb`).
        A missing file → assume near-budget rather than 0, so admission never fails open."""
        try:
            size_gb = os.path.getsize(self.model) / (1024 ** 3)
        except OSError:
            return self.vram_budget_gb * 0.95
        return size_gb * 1.2 + 1.0

    def spawn(self):
        """Start `llama-server` on a fresh dynamic port; return the Popen (the shared lifecycle
        records its pid on admission and drives readiness/teardown)."""
        self._port = _free_port()
        return subprocess.Popen(
            [self.llama_bin, "-m", self.model, "--host", "127.0.0.1", "--port", str(self._port),
             "-ngl", self.ngl, "--ctx-size", self.ctx, "--alias", self.alias],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ready(self) -> bool:
        if self._port is None:
            return False
        try:
            with urllib.request.urlopen(self._url("/health"), timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    # -- forward surface (engine-specific; the agent calls these while holding the runtime lock) -
    def forward(self, verb: str, body: dict, load_ms: float) -> dict:
        """Non-streaming inference. Byte-compatible response with the retired supervisor's `/infer`
        ({text, load_ms, infer_ms, model, usage})."""
        if verb != "infer":
            raise ValueError(f"llm engine has no verb {verb!r}")
        payload, _ = self._chat_payload(body, stream=False)
        req = urllib.request.Request(self._url("/v1/chat/completions"), data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
        return {
            "text": data["choices"][0]["message"]["content"],
            "load_ms": load_ms,
            "infer_ms": round((time.perf_counter() - t0) * 1000, 1),
            "model": self.alias,
            "usage": data.get("usage", {}),
        }

    def stream(self, verb: str, body: dict, load_ms: float):
        """SSE generator of byte-chunks: start → token* → done (verbatim from the supervisor's
        `_do_infer_stream`, so the gateway's raw-passthrough proxy stays byte-identical)."""
        if verb != "infer":
            raise ValueError(f"llm engine has no verb {verb!r}")
        payload, _ = self._chat_payload(body, stream=True)
        req = urllib.request.Request(self._url("/v1/chat/completions"), data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        yield self._frame({"event": "start", "load_ms": load_ms, "model": self.alias})
        ntok = 0
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0].get("delta", {}).get("content")
                except Exception:
                    delta = None
                if delta:
                    ntok += 1
                    yield self._frame({"event": "token", "text": delta})
        infer_ms = round((time.perf_counter() - t0) * 1000, 1)
        yield self._frame({"event": "done", "infer_ms": infer_ms, "tokens": ntok,
                           "model": self.alias})

    def health(self, resident: bool) -> dict:
        """Byte-compatible with the retired supervisor's `/health` (gateway `serving.gpu_state`
        reads `ok`/`resident`/`model`/`lease_holder`). `lease_holder` + `vram_free_gb` come from the
        GLOBAL lockfile during migration (see the module note); None once the lease retires."""
        holder = self._lease.current_holder() if self._lease else None
        free = self._lease.free_vram_gb() if self._lease else None
        est = self.estimate_vram() if os.path.exists(self.model) else None
        return {
            "ok": True,
            "resident": resident,
            "model": self.alias,
            "vram_budget_gb": self.vram_budget_gb,
            "est_vram_gb": round(est, 1) if est is not None else None,
            "fits": (est <= self.vram_budget_gb * 0.95) if est is not None else None,
            "vram_free_gb": round(free, 1) if free is not None else None,
            "lease_holder": holder.get("tenant") if holder else None,
        }

    # -- helpers ---------------------------------------------------------------------------------
    def _chat_payload(self, body: dict, *, stream: bool):
        prompt = body.get("prompt", "")
        max_tokens = int(body.get("max_tokens", 256))
        temperature = float(body.get("temperature", 0.7))
        payload = {"model": self.alias,
                   "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens, "temperature": temperature}
        if stream:
            payload["stream"] = True
        return json.dumps(payload).encode(), (max_tokens, temperature)

    @staticmethod
    def _frame(event: dict) -> bytes:
        return f"data: {json.dumps(event)}\n\n".encode()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"
