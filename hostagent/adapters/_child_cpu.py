"""Shared base for the CPU child engines (018 US2, T361; 020 T410 — `_bento_cpu` renamed, launch
flipped to the slim children) — embed + tabular.

Both are off-lease CPU children (`gpu=False` → the shared lifecycle never touches admission for
them; "one model in VRAM" is about VRAM, and these hold none). Each run.sh is wrapped as an
agent child exactly like vision, minus the GPU lease: a dynamic-port spawn in a process group,
a `/readyz` probe, and a JSON forward that relays the request body to the child's endpoint
verbatim (byte-compat, FR-177). `embed.py` / `tabular.py` are thin subclasses that set the
engine id, verb (== the child route), and run script — one definition of the CPU-child
behaviour, no copy-drift (the theme the 2026-07 architecture review set for 018, §4.2). 020
US2 swaps the BentoML children for slim FastAPI ones (serving/children/); the adapter contract
(verbs, states, error mapping) is untouched — only the launch path + pip hint moved.
"""
import json
import os
import urllib.request

from hostagent.adapters._common import bento_spawn, http_200

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ChildCpuAdapter:
    gpu = False        # CPU, off-lease — the lifecycle admits nothing and never idle-reaps for VRAM
    optional = False
    stream_verbs = ()
    #: subclasses set these three:
    engine_id = None
    verb = None         # the agent verb == the child route (POST /<verb> on the child)
    run_script = None   # e.g. "embed_run.sh"

    def __init__(self, admission=None):
        # `admission` is accepted for a uniform factory signature but unused — CPU engines are
        # off-lease (they hold no VRAM, so their /health carries no holder field).
        self.venv = os.path.expanduser(os.getenv("VENV", "~/mlops-train"))
        self.uvicorn_bin = os.path.join(self.venv, "bin", "uvicorn")
        self.run_sh = os.path.join(_REPO, "serving", "children", self.run_script)
        self._port = None

    @property
    def verbs(self):
        return (self.verb,)

    def available(self):
        if not (os.path.isfile(self.uvicorn_bin) and os.access(self.uvicorn_bin, os.X_OK)):
            return (False, f"uvicorn not found in {self.venv} — pip install -r "
                           f"serving/children/requirements.txt into the venv")
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
