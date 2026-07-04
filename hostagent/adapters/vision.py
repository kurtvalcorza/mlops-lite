"""Vision engine adapter (018 US2, T360; 020 T410 — launch flipped to the slim child).

The adapter spawns the vision child via `serving/children/run.sh` (020 US2: a slim FastAPI
single-route service — the BentoML framework is retired, FR-203/FR-204) on a dynamic loopback
port, probes `/readyz`, and relays the classify request. The adapter CONTRACT is unchanged from
the fold-in: admission is the agent's job — `EngineRuntime` acquires the `vision` lease before
spawning the child and killing the child frees the VRAM (the child loads the torch model
in-process; the agent stays torch-free, R10).

uvicorn may still fork/manage workers, so the child stays spawned in its own session (process
group) and terminate/kill signal the WHOLE group. `classify` is a multipart image upload (the
gateway forwards it verbatim); the adapter relays the raw multipart body + Content-Type to the
child unchanged (byte-compat, FR-177).
"""
import json
import os
import urllib.request

from platformlib.topology import vram_budget_gb

from hostagent.adapters._common import bento_spawn, engine_health, http_200

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class VisionAdapter:
    engine_id = "vision"
    gpu = True
    optional = False
    verbs = ("classify",)
    stream_verbs = ()

    def __init__(self, admission=None):
        self.venv = os.path.expanduser(os.getenv("VENV", "~/mlops-train"))
        self.uvicorn_bin = os.path.join(self.venv, "bin", "uvicorn")
        self.run_sh = os.path.join(_REPO, "serving", "children", "run.sh")
        self.model_name = os.getenv("VISION_MODEL", "vision-mobilenet")
        self.est_gb = float(os.getenv("VISION_EST_GB", "1.0"))
        self.vram_budget_gb = vram_budget_gb()  # FR-207: the single shared budget resolver
        self._admission = admission
        self._port = None

    # -- lifecycle interface --------------------------------------------------------------------
    def available(self):
        if not (os.path.isfile(self.uvicorn_bin) and os.access(self.uvicorn_bin, os.X_OK)):
            return (False, f"uvicorn not found in {self.venv} — "
                           f"pip install -r serving/children/requirements.txt into the venv")
        if not os.path.isfile(self.run_sh):
            return (False, f"vision run script missing at {self.run_sh}")
        return (True, None)

    def estimate_vram(self) -> float:
        return self.est_gb  # MobileNet + CUDA context (small, non-zero)

    def spawn(self):
        """Spawn the slim child via its run.sh (sources .env + bridges store creds) on a dynamic
        loopback port, in its own process group so the whole worker tree is reap-able (_common)."""
        self._port, child = bento_spawn(self.run_sh)
        return child

    def ready(self) -> bool:
        return self._port is not None and http_200(self._url("/readyz"))

    # -- forward surface (multipart, not JSON) --------------------------------------------------
    def forward_multipart(self, verb: str, raw_body: bytes, content_type: str, load_ms: float):
        """Relay the raw multipart image upload to the child's /classify unchanged. Response
        {model, device, predictions} is byte-identical to the retired daemon (FR-177)."""
        if verb != "classify":
            raise ValueError(f"vision engine has no verb {verb!r}")
        req = urllib.request.Request(self._url("/classify"), data=raw_body,
                                     headers={"Content-Type": content_type}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def health(self, resident: bool) -> dict:
        ok, reason = self.available()
        return engine_health(self._admission, ok=ok, reason=reason, resident=resident,
                             model=self.model_name, vram_budget_gb=self.vram_budget_gb,
                             est_vram=self.est_gb if ok else None)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"
