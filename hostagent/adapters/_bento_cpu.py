"""Shared base for the CPU BentoML engines (018 US2, T361) — embed + tabular.

Both are off-lease CPU BentoML services (`gpu=False` → the shared lifecycle never touches admission
for them; "one model in VRAM" is about VRAM, and these hold none). The fold-in wraps each run.sh as
an agent child (research R10) exactly like vision, minus the GPU lease: a dynamic-port spawn in a
process group, a `/readyz` probe, and a JSON forward that relays the request body to the child's
endpoint verbatim (byte-compat, FR-177). `embed.py` / `tabular.py` are thin subclasses that set the
engine id, verb (== the child route), and run script — one definition of the CPU-bento behaviour, no
copy-drift (the theme the 2026-07 architecture review set for 018, §4.2).
"""
import json
import os
import urllib.request

from hostagent.adapters._common import bento_spawn, http_200

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BentoCpuAdapter:
    gpu = False        # CPU, off-lease — the lifecycle admits nothing and never idle-reaps for VRAM
    optional = False
    stream_verbs = ()
    #: subclasses set these three:
    engine_id = None
    verb = None         # the agent verb == the bento child route (POST /<verb> on the child)
    run_script = None   # e.g. "embed_run.sh"
    _pip = ""           # extra deps named in the `unavailable` hint

    def __init__(self, admission=None):
        # `admission` is accepted for a uniform factory signature but unused — CPU engines are
        # off-lease (they hold no VRAM, so their /health carries no holder field).
        self.venv = os.path.expanduser(os.getenv("VENV", "~/mlops-train"))
        self.bentoml_bin = os.path.join(self.venv, "bin", "bentoml")
        self.run_sh = os.path.join(_REPO, "serving", "bento", self.run_script)
        self._port = None

    @property
    def verbs(self):
        return (self.verb,)

    def available(self):
        if not (os.path.isfile(self.bentoml_bin) and os.access(self.bentoml_bin, os.X_OK)):
            return (False, f"bentoml not found in {self.venv} — pip install bentoml {self._pip}")
        if not os.path.isfile(self.run_sh):
            return (False, f"{self.engine_id} run script missing at {self.run_sh}")
        return (True, None)

    def estimate_vram(self):
        return 0.0  # CPU engine — no VRAM (never reaches admission anyway)

    def spawn(self):
        self._port, child = bento_spawn(self.run_sh)
        return child

    def ready(self):
        return self._port is not None and http_200(self._url("/readyz"))

    def forward(self, verb, body, load_ms):
        """Relay the JSON body to the child's /<verb> endpoint verbatim; return its JSON (a vectors
        list for embed, a predictions dict for tabular) unchanged (byte-compat, FR-177)."""
        if verb != self.verb:
            raise ValueError(f"{self.engine_id} engine has no verb {verb!r}")
        data = json.dumps(body).encode()
        req = urllib.request.Request(self._url(f"/{self.verb}"), data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def health(self, resident):
        ok, reason = self.available()
        p = {"ok": ok, "resident": resident, "engine": self.engine_id, "device": "cpu"}
        if not ok:
            p["unavailable"] = reason
        return p

    def _url(self, path):
        return f"http://127.0.0.1:{self._port}{path}"
