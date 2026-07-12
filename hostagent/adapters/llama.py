"""LLM engine adapter (018 US2, T358; 022 US1/US3, T465) — llama.cpp, registry-driven.

The load-on-demand → ready → drain → idle-release → reap lifecycle lives ONCE in
`hostagent.lifecycle`; this adapter supplies only the llama-specific bits: spawn a `llama-server`
on a **dynamic** port, probe its readiness, and forward inference — REST and SSE — to it.
Stdlib-only (the agent carries no pip deps; the resolver's mlflow/boto3 load lazily).

022 (FR-254/255): the served artifact is REGISTRY-RESOLVED at each cold load — `rebind()` asks
`hostagent.serving_llm` for the active serving-LLM and binds `self.model` (base GGUF),
`self.lora` (adapter GGUF or None) and the response identity (`self.alias` = the registry model
name, `self.registry_version`) before `spawn()` builds the command line. The `MODEL`/`LORA`/
`MODEL_ALIAS` env knobs are now the FALLBACK DEFAULT only — they serve when the pointer is unset
(byte-for-byte today's behavior, SC-149) or when resolution infra is unreachable (surfaced in
health as `binding`, never silent). A pointer that names an UNRESOLVABLE target makes the engine
unavailable (fail loud, FR-265) — the swap target-probe then refuses rather than evicting a
working holder for a load that would fail.

Byte-compatibility (FR-177/FR-271): the forward request/response bodies and the SSE frames keep
the retired supervisor's exact shapes; identity fields in /health are additive.

Health source (T364): `admission` is the single in-process GPU authority, read ONLY to build the
byte-compatible `/engines/llm/health` GLOBAL holder + free-VRAM fields.
"""
import json
import os
import subprocess
import time
import urllib.request

from hostagent.adapters._common import engine_health, free_port
from platformlib.topology import vram_budget_gb

DEFAULT_MODEL = "~/models/gguf/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
DEFAULT_ALIAS = "qwen2.5-7b-instruct-q4_k_m"

#: How long a registry binding stays fresh (s). available() is on the hot /health path — the TTL
#: bounds pointer/registry reads to one per window; the promote-triggered reload forces a fresh
#: resolve regardless (rebind(force=True)), so a switch is never TTL-delayed.
REBIND_TTL_S = float(os.getenv("LLM_REBIND_TTL_S", "5"))

#: Sentinel distinguishing "argument omitted" from an explicit `lora_path=None` (no adapter) in
#: estimate_vram — None is a valid value, so it can't double as "use the current binding".
_UNSET = object()


class LlamaAdapter:
    """Adapter for the text-generation (llm) engine. Implements the duck-typed
    `data-model.md §EngineAdapter` interface plus the engine-specific forward surface the agent
    routes to (`verbs` / `stream_verbs` / `forward` / `stream` / `health`)."""

    engine_id = "llm"
    gpu = True
    optional = False
    verbs = ("infer",)         # POST /engines/llm/infer
    stream_verbs = ("infer",)  # POST /engines/llm/infer/stream (SSE)

    def __init__(self, admission=None, resolver=None):
        self.llama_bin = os.path.expanduser(
            os.getenv("LLAMA_BIN", "~/llama.cpp/build/bin/llama-server"))
        # Env-configured DEFAULTS (022): what serves when no serving-LLM is selected. LORA is the
        # generalized prototype knob — a default adapter, normally unset.
        self._env_model = os.path.expanduser(os.getenv("MODEL", DEFAULT_MODEL))
        self._env_lora = os.path.expanduser(os.getenv("LORA")) if os.getenv("LORA") else None
        self._env_alias = os.getenv("MODEL_ALIAS", DEFAULT_ALIAS)
        # The LIVE binding (what the next spawn serves) — starts at the defaults; rebind() moves it.
        self.model = self._env_model
        self.lora = self._env_lora
        self.alias = self._env_alias
        self.registry_version = None
        self.ngl = os.getenv("NGL", "999")
        self.ctx = os.getenv("CTX", "4096")
        self.vram_budget_gb = vram_budget_gb()  # FR-207: the single shared budget resolver
        self._admission = admission
        self._resolver = resolver  # injectable for tests; None → hostagent.serving_llm.resolve
        self._bind_error = None    # ResolutionError text → available() reports (False, reason)
        self._bind_note = None     # degradation note (resolution infra unreachable → env default)
        self._bound_at = None      # monotonic time of the last successful rebind (TTL)
        self._loaded = None        # identity captured at spawn() — what is ACTUALLY resident
        self._port = None          # set on spawn(); the child's dynamic port

    # -- 022 registry binding (T465) ---------------------------------------------------------------
    def rebind(self, force: bool = False) -> None:
        """Resolve the active serving-LLM and bind base/adapter/identity for the next spawn.

        TTL-cached (REBIND_TTL_S) because available() runs on every health poll; the reload verb
        passes force=True so a promote is never stale. Never raises — a ResolutionError is recorded
        in `_bind_error` (available() fails loud), an unreachable store/registry falls back to the
        env defaults with `_bind_note` set (surfaced, honest: identity reports the actual binding).
        """
        now = time.monotonic()
        if not force and self._bound_at is not None and (now - self._bound_at) < REBIND_TTL_S:
            return
        resolver = self._resolver
        if resolver is None:
            from hostagent import serving_llm
            resolver = serving_llm.resolve
        from hostagent import serving_llm as _sl
        try:
            target = resolver()
        except _sl.ResolutionError as e:
            self._bind_error, self._bind_note = str(e), None
            self._bound_at = now
            return
        except _sl.ResolutionUnavailable as e:
            self._bind_to_env(note=str(e))
            self._bound_at = now
            return
        except Exception as e:  # noqa: BLE001 — a resolver bug must not take the health surface down
            self._bind_to_env(note=f"unexpected resolution failure: {e}")
            self._bound_at = now
            return
        self._bind_error = None
        if target is None:  # pointer unset → the configured default base (today's behavior)
            self._bind_to_env(note=None)
        else:
            self.model = os.path.expanduser(target["base_gguf"])
            self.lora = os.path.expanduser(target["adapter_gguf"]) if target["adapter_gguf"] \
                else None
            self.alias = target["model_name"]
            self.registry_version = target["version"]
            self._bind_note = None
        self._bound_at = now

    def _bind_to_env(self, note):
        self.model, self.lora = self._env_model, self._env_lora
        self.alias, self.registry_version = self._env_alias, None
        self._bind_error, self._bind_note = None, note

    def bound_identity(self) -> tuple:
        """(model_name, registry_version) the NEXT spawn would serve — the reload verb compares
        this to loaded_identity() for the idempotent no-op (FR-256)."""
        return (self.alias, self.registry_version)

    def snapshot_binding(self) -> dict:
        """Capture the current binding so a failed same-tenant reload can restore the PRIOR working
        model (022 T466 rollback — data-model.md edge case: 'serving is never left empty'). Taken
        BEFORE `rebind(force=True)` overwrites the fields, so it holds the model that was serving."""
        return {"model": self.model, "lora": self.lora, "alias": self.alias,
                "registry_version": self.registry_version,
                "bind_error": self._bind_error, "bind_note": self._bind_note,
                "loaded": self._loaded}

    def restore_binding(self, snap: dict) -> None:
        """Restore a `snapshot_binding()` and FREEZE it (fresh `_bound_at`) so the immediate
        recovery reload re-spawns the restored model rather than re-resolving the still-bad target
        that just failed. The TTL then lets a later request re-resolve normally once the operator
        has acted on the surfaced failure."""
        self.model, self.lora = snap["model"], snap["lora"]
        self.alias, self.registry_version = snap["alias"], snap["registry_version"]
        self._bind_error, self._bind_note = snap["bind_error"], snap["bind_note"]
        self._loaded = snap["loaded"]
        self._bound_at = time.monotonic()

    def loaded_identity(self):
        """(model_name, registry_version) of the resident child, or None when never spawned."""
        return (self._loaded["model_name"], self._loaded["registry_version"]) \
            if self._loaded else None

    # -- lifecycle interface (data-model.md §EngineAdapter) --------------------------------------
    def available(self):
        """Binary + resolved artifacts present AND usable? Surfaces as `unavailable(reason)` (R7)
        instead of a spawn crash. Rebinds first (TTL-bounded) so the swap/reload target-probe
        checks the artifacts that WOULD load — an unresolvable serving-LLM target or a missing
        base/adapter GGUF refuses here rather than evicting a working holder (FR-265)."""
        self.rebind()
        if self._bind_error:
            return (False, f"serving-LLM target unresolvable: {self._bind_error}")
        if not (os.path.isfile(self.llama_bin) and os.access(self.llama_bin, os.X_OK)):
            return (False, f"llama-server missing or not executable at {self.llama_bin} — "
                           f"build llama.cpp (README)")
        if not os.path.isfile(self.model):
            return (False, f"model GGUF not found (or not a file) at {self.model}")
        if self.lora and not os.path.isfile(self.lora):
            return (False, f"LoRA adapter GGUF not found (or not a file) at {self.lora}")
        return (True, None)

    def estimate_vram(self, base_path=None, lora_path=_UNSET) -> float:
        """Quantized weights + KV/context/overhead headroom (the supervisor's `_estimate_vram_gb`),
        plus the adapter's weights when one is bound. A missing file → assume near-budget rather
        than 0, so admission never fails open. Defaults to the CURRENT binding; `base_path`/
        `lora_path` override it to estimate the RESIDENT artifacts for a resident health report
        (so the reported VRAM tracks the loaded child, not a pending rebind target)."""
        base_path = base_path or self.model
        lora_path = self.lora if lora_path is _UNSET else lora_path
        try:
            size_gb = os.path.getsize(base_path) / (1024 ** 3)
        except OSError:
            return self.vram_budget_gb * 0.95
        if lora_path:
            try:
                size_gb += os.path.getsize(lora_path) / (1024 ** 3)
            except OSError:
                return self.vram_budget_gb * 0.95
        return size_gb * 1.2 + 1.0

    def spawn(self):
        """Start `llama-server` on a fresh dynamic port; return the Popen (the shared lifecycle
        records its pid on admission and drives readiness/teardown). Serves the CURRENT binding —
        `-m <base> [--lora <adapter>]` (022 US3, FR-263) — and captures it as the loaded identity
        the health surface reports (FR-260)."""
        self._port = free_port()
        cmd = [self.llama_bin, "-m", self.model, "--host", "127.0.0.1",
               "--port", str(self._port), "-ngl", self.ngl, "--ctx-size", self.ctx,
               "--alias", self.alias]
        if self.lora:
            cmd += ["--lora", self.lora]
        self._loaded = {"model_name": self.alias, "registry_version": self.registry_version,
                        "base": os.path.basename(self.model),
                        "adapter": os.path.basename(self.lora) if self.lora else None,
                        # full paths so a resident health report estimates the RESIDENT artifacts,
                        # not `self.model` (which a later poll's rebind may move to a pending target)
                        "base_path": self.model, "adapter_path": self.lora}
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        ({text, load_ms, infer_ms, model, usage}); `model` names the registry model actually
        serving (FR-262 — the loaded identity, not a static env alias)."""
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
            "model": self._served_name(),
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
        yield self._frame({"event": "start", "load_ms": load_ms, "model": self._served_name()})
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
                           "model": self._served_name()})

    def health(self, resident: bool) -> dict:
        """The shared GPU-tenant payload (`_common.engine_health`) plus the 022 honest-identity
        fields (contracts/agent-identity-and-allowlist.md): `model_name` + `registry_version` name
        what is ACTUALLY resident when resident, else what the next load would serve; `base` /
        `adapter` carry the resolved artifact identities for a served fine-tune (FR-274)."""
        resident_child = resident and self._loaded
        if resident_child:
            # A live resident child is serving NOW (Codex F1). Do NOT probe/rebind a PENDING pointer
            # target here: a promote that wrote a bad/missing target whose reload was REFUSED leaves
            # this child intentionally resident, and letting `available()` flip ok=False would 503
            # the gateway (serving.health() gates /infer on this) into an OUTAGE while a healthy
            # child keeps serving. Report the resident child — it's alive, hence ok; identity +
            # artifacts + VRAM estimate all from `_loaded` (self.model may already be a pending
            # target after a poll's state() rebind).
            ok, reason = True, None
            bp = self._loaded.get("base_path") or self.model
            est = self.estimate_vram(bp, self._loaded.get("adapter_path")) \
                if os.path.isfile(bp) else None
        else:
            ok, reason = self.available()  # cold: resolve + probe the next target (picks up a promote)
            est = self.estimate_vram() if os.path.isfile(self.model) else None
        ident = self._loaded if resident_child else {
            "model_name": self.alias, "registry_version": self.registry_version,
            "base": os.path.basename(self.model),
            "adapter": os.path.basename(self.lora) if self.lora else None}
        payload = engine_health(self._admission, ok=ok, reason=reason, resident=resident,
                                model=ident["model_name"], vram_budget_gb=self.vram_budget_gb,
                                est_vram=est)
        payload.update({"model_name": ident["model_name"],
                        "registry_version": ident["registry_version"],
                        "base": ident["base"], "adapter": ident["adapter"]})
        if self._bind_note:
            payload["binding"] = f"registry resolution unavailable — serving the configured " \
                                 f"default ({self._bind_note})"
        return payload

    # -- helpers ---------------------------------------------------------------------------------
    def _served_name(self) -> str:
        """The model name for a response: the LOADED identity (a forward always runs against the
        resident child), falling back to the binding before any spawn."""
        return self._loaded["model_name"] if self._loaded else self.alias

    def _chat_payload(self, body: dict, *, stream: bool):
        prompt = body.get("prompt", "")
        max_tokens = int(body.get("max_tokens", 256))
        temperature = float(body.get("temperature", 0.7))
        payload = {"model": self._served_name(),
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
