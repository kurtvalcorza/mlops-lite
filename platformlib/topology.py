"""Tenant identities, port/URL topology, and the host state dir (018 T343, FR-176).

Replaces the string literals scattered across the gateway and the native daemons (review §4.3):
tenant names and the holder→URL map all resolve here. The strangler migration is complete
(T364): the native daemons are gone, so a single `AGENT_URL` is the only endpoint — the six
per-daemon URL resolvers and their hand-allocated ports (8090–8095/8099) retired with the lockfile.
"""
import os


class Tenant:
    """Canonical GPU tenant identities (data-model.md §Tenant)."""
    LLM = "llm"
    ASR = "asr"
    VISION = "vision"
    TRAINING = "training"


#: Serving-tenant labels (preemptable by an operator-confirmed swap). `training` is intentionally
#: absent — a running training/HPO/batch job is never preempted (FR-172, constitution Principle II).
SERVING_TENANTS = (Tenant.LLM, Tenant.ASR, Tenant.VISION)

#: The SINGLE definition the agent's swap logic consults (contracts/platformlib.md, FR-172): an
#: admission holder whose `kind` is listed here structurally refuses preemption — no network
#: probe, no fail-open path. Kind-based (not tenant-based) so a batch/HPO job under a serving
#: tenant id is protected too.
NON_PREEMPTABLE_KINDS = {"job"}

#: Engine registry (data-model.md §EngineAdapter). `gpu=False` engines never touch admission;
#: `optional=True` engines are excluded from platform-health's `all_healthy` (research R7).
#: `ready_wait_s` is the per-engine cold-start budget (019/US8, FR-196): the native llm/asr children
#: warm in well under a minute, but the BentoML engines (vision/embed/tabular) import BentoML and can
#: download model weights on a fresh host — the retired supervisors gave them ~120s. A uniform 60s
#: default killed a slow first load and re-downloaded on every retry. `{ENGINE}_READY_WAIT_S` /
#: `{ENGINE}_IDLE_TIMEOUT_S` env vars override per engine (build_runtimes).
ENGINES = {
    "llm":     {"gpu": True,  "optional": False, "ready_wait_s": 60},
    "asr":     {"gpu": True,  "optional": True,  "ready_wait_s": 60},
    "vision":  {"gpu": True,  "optional": False, "ready_wait_s": 120},
    "embed":   {"gpu": False, "optional": False, "ready_wait_s": 120},
    "tabular": {"gpu": False, "optional": False, "ready_wait_s": 120},
}

#: Modalities with a fine-tune flow (the on-breach retrain targets, FR-181). The canonical shared
#: definition — `training/flow_dispatch.VALID_MODALITIES` mirrors it until the jobs fold-in (T362).
TRAINABLE_MODALITIES = ("llm", "vision", "embeddings", "asr")

#: The host agent's single stable endpoint (research R3) — the ONLY port the platform binds now
#: that every engine + the jobs surface is folded into the agent (the legacy daemon ports
#: 8090–8095/8099 retired at T364).
AGENT_PORT = 8100

#: Fixed, reboot-stable host state dir (FR-166): the transitional lease + beacon, the agent
#: journal, and agent logs. NEVER under /tmp — a reboot or a per-distro mount namespace would
#: silently void the GPU mutex (review §4.6). Env override for tests and non-default layouts.
STATE_DIR = os.path.expanduser(os.getenv("MLOPS_STATE_DIR", "~/.mlops-lite"))


def _url(env_name: str, default_port: int) -> str:
    return os.getenv(env_name) or f"http://host.docker.internal:{default_port}"


def agent_url() -> str:
    return _url("AGENT_URL", AGENT_PORT)
