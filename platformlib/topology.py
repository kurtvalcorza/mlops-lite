"""Tenant identities, port/URL topology, and the host state dir (018 T343, FR-176).

Replaces the string literals scattered across the gateway and the native daemons (review §4.3):
tenant names, the holder→URL maps, and the hand-allocated port list all resolve here. During the
strangler migration each engine fold-in flips ONE env var (`SERVING_URL` → the agent) — see
research R3; at completion a single `AGENT_URL` replaces the six daemon URLs.
"""
import os


class Tenant:
    """Canonical GPU-lease tenant identities (data-model.md §Tenant).

    `LLM_LEGACY` is the pre-018 lease string the llama supervisor still writes; the gateway's
    holder-label mapping collapses it to `LLM`. It is deleted with the lockfile (T364)."""
    LLM = "llm"
    ASR = "asr"
    VISION = "vision"
    TRAINING = "training"
    LLM_LEGACY = "llm-serving"


#: Serving-tenant labels (preemptable by an operator-confirmed swap). `training` is intentionally
#: absent — a running training/HPO/batch job is never preempted (FR-172, constitution Principle II).
SERVING_TENANTS = (Tenant.LLM, Tenant.ASR, Tenant.VISION)
NON_PREEMPTABLE = {Tenant.TRAINING}

#: Engine registry (data-model.md §EngineAdapter). `gpu=False` engines never touch admission;
#: `optional=True` engines are excluded from platform-health's `all_healthy` (research R7).
ENGINES = {
    "llm":     {"gpu": True,  "optional": False},
    "asr":     {"gpu": True,  "optional": True},
    "vision":  {"gpu": True,  "optional": False},
    "embed":   {"gpu": False, "optional": False},
    "tabular": {"gpu": False, "optional": False},
}

#: Modalities with a fine-tune flow (the on-breach retrain targets, FR-181). The canonical shared
#: definition — `training/flow_dispatch.VALID_MODALITIES` mirrors it until the jobs fold-in (T362).
TRAINABLE_MODALITIES = ("llm", "vision", "embeddings", "asr")

#: The host agent's single stable endpoint (research R3). Legacy daemon ports 8090–8095/8099 are
#: retired per fold-in phase and deleted from here at lockfile retirement (T364).
AGENT_PORT = 8100

#: Fixed, reboot-stable host state dir (FR-166): the transitional lease + beacon, the agent
#: journal, and agent logs. NEVER under /tmp — a reboot or a per-distro mount namespace would
#: silently void the GPU mutex (review §4.6). Env override for tests and non-default layouts.
STATE_DIR = os.path.expanduser(os.getenv("MLOPS_STATE_DIR", "~/.mlops-lite"))


def _url(env_name: str, default_port: int) -> str:
    return os.getenv(env_name) or f"http://host.docker.internal:{default_port}"


def agent_url() -> str:
    return _url("AGENT_URL", AGENT_PORT)


# Legacy daemon URLs — one resolver per engine so a fold-in phase flips exactly one env var.
def serving_url() -> str:
    return _url("SERVING_URL", 8090)


def trainer_url() -> str:
    return _url("TRAINER_URL", 8091)


def bento_url() -> str:
    return _url("BENTO_URL", 8092)


def embed_url() -> str:
    return _url("EMBED_URL", 8093)


def tabular_url() -> str:
    return _url("TABULAR_URL", 8094)


def asr_url() -> str:
    return _url("ASR_URL", 8095)
