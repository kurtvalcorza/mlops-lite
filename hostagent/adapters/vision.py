"""Vision engine adapter (018 US2, T360) — wraps the BentoML vision service as an agent child.

Unlike the llama/whisper adapters (which spawn a single binary), vision is an existing BentoML
service. Per research R10 the fold-in WRAPS it: the adapter spawns `serving/bento/run.sh` on a
dynamic loopback port, probes BentoML's `/readyz`, and relays the classify request. The service's
own in-process GPU-lease code is stripped in the same phase (T360) — admission is the agent's job
now, so the adapter's `EngineRuntime` acquires the `vision` lease before spawning the child and
killing the child frees the VRAM (BentoML loads the torch model in-process; the agent stays
torch-free, R10).

BentoML forks worker processes, so the child is spawned in its own session (process group) and
terminate/kill signal the WHOLE group — killing only the launcher would orphan a worker still
holding VRAM. `classify` is a multipart image upload (the gateway forwards it verbatim); the adapter
relays the raw multipart body + Content-Type to the child unchanged (byte-compat, FR-177).
"""
import json
import os
import urllib.request

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
        self.bentoml_bin = os.path.join(self.venv, "bin", "bentoml")
        self.run_sh = os.path.join(_REPO, "serving", "bento", "run.sh")
        self.model_name = os.getenv("VISION_MODEL", "vision-mobilenet")
        self.est_gb = float(os.getenv("VISION_EST_GB", "1.0"))
        self.vram_budget_gb = float(os.getenv("VRAM_GB", "12"))
        self._admission = admission
        self._port = None

    # -- lifecycle interface --------------------------------------------------------------------
    def available(self):
        if not (os.path.isfile(self.bentoml_bin) and os.access(self.bentoml_bin, os.X_OK)):
            return (False, f"bentoml not found in {self.venv} — "
                           f"pip install bentoml torchvision pillow into the venv")
        if not os.path.isfile(self.run_sh):
            return (False, f"vision run script missing at {self.run_sh}")
        return (True, None)

    def estimate_vram(self) -> float:
        return self.est_gb  # MobileNet + CUDA context (small, non-zero)

    def spawn(self):
        """Spawn the bento service via its run.sh (sources .env + bridges MinIO creds) on a dynamic
        loopback port, in its own process group so the whole worker tree is reap-able (_common)."""
        self._port, child = bento_spawn(self.run_sh)
        return child

    def ready(self) -> bool:
        return self._port is not None and http_200(self._url("/readyz"))

    # -- forward surface (multipart, not JSON) --------------------------------------------------
    def forward_multipart(self, verb: str, raw_body: bytes, content_type: str, load_ms: float):
        """Relay the raw multipart image upload to the bento child's /classify unchanged. Response
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
