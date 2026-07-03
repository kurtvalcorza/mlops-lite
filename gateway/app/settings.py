"""Central gateway settings (018 T344, FR-176) — env is read HERE, once, via platformlib.

Before 018 the daemon URLs were re-read via `os.getenv` in seven-plus modules (review §4.3);
adding a daemon or changing a default meant editing many files. Modules now do
`from ..settings import TRAINER_URL` (routers) / `from .settings import ...` and keep a
module-level alias so tests can still monkeypatch `module.TRAINER_URL`.

Deliberate exceptions (NOT consolidated here — they must stay standalone-loadable, i.e. free of
relative imports at module top level):
  - `swap.py` — loaded in isolation by the offline swap tests (importlib, no package context).
  - `quality.py` / `batch.py` / `shadow.py` — dual-runtime modules also loaded by the trainer.
  - `evaluation.py` — loaded standalone by training/scoring and the HPO objective.
Those keep their env reads until their 018 fold-in phases retire the dual-runtime hacks
(tasks T362/T374).
"""
from platformlib import topology

AGENT_URL = topology.agent_url()

SERVING_URL = topology.serving_url()
TRAINER_URL = topology.trainer_url()
BENTO_URL = topology.bento_url()
EMBED_URL = topology.embed_url()
TABULAR_URL = topology.tabular_url()
ASR_URL = topology.asr_url()

import os as _os  # noqa: E402 — the two non-topology settings

SERVING_MODEL = _os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")
MLFLOW_TRACKING_URI = _os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
