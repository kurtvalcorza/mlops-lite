"""Central gateway settings (018 T344, FR-176) — env is read HERE, once, via platformlib.

Before 018 the daemon URLs were re-read via `os.getenv` in seven-plus modules (review §4.3);
adding a daemon or changing a default meant editing many files. Modules now do
`from ..settings import TRAINER_URL` (routers) / `from .settings import ...` and keep a
module-level alias so tests can still monkeypatch `module.TRAINER_URL`.

T364 (lockfile retirement): the native daemons are gone — every engine + the jobs surface is the
ONE host agent. So there is a single source, `AGENT_URL`, and the per-engine names below are
DERIVED from it: each router keeps its byte-compatible path (`/infer`, `/classify`, `/train`, …)
under the agent's `/engines/<id>` sub-path (or the agent root for the jobs/trainer surface). The
six separate `*_URL` env vars + the six `topology.*_url()` resolvers retired with the lockfile;
compose/up_all now inject only `AGENT_URL`.

Deliberate exceptions (NOT consolidated here — they must stay standalone-loadable, i.e. free of
relative imports at module top level):
  - `quality.py` / `batch.py` / `shadow.py` — dual-runtime modules also loaded by the trainer flows.
  - `evaluation.py` — loaded standalone by training/scoring and the HPO objective.
Those keep their env reads until their 018 fold-in phases retire the dual-runtime hacks (task T374).
(`swap.py` was deleted at T363 — the gateway no longer brokers preemption; the agent orchestrates.)
"""
from platformlib import topology

AGENT_URL = topology.agent_url()

# Derived per-engine base URLs (T364): one agent, one endpoint. The routers append their existing
# byte-compatible verb paths to these (e.g. SERVING_URL + "/infer" → the agent's /engines/llm/infer).
SERVING_URL = f"{AGENT_URL}/engines/llm"
BENTO_URL = f"{AGENT_URL}/engines/vision"
EMBED_URL = f"{AGENT_URL}/engines/embed"
TABULAR_URL = f"{AGENT_URL}/engines/tabular"
ASR_URL = f"{AGENT_URL}/engines/asr"
TRAINER_URL = AGENT_URL  # the jobs surface serves the legacy /train|/study|/batch|/shadow-replay aliases

import os as _os  # noqa: E402 — the two non-topology settings

SERVING_MODEL = _os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")
MLFLOW_TRACKING_URI = _os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
