"""Endpoints, synthesized (027 T748/T749 — data-model §7, research R7).

There is no endpoint table and this adds none. An "endpoint" on this platform is a *derived* view of
four things that already exist: the registry's serving alias (what is desired), the serving pointer
and activation record (what is in flight), the agent's engine list (what is actually resident), and
the modality's engine wiring. Persisting it would create a fifth system of record whose only job is
to disagree with the other four.

**The status rule is the module** (FR-415/416/417):

  * `healthy` requires **resident** confirmation. Desired-only is `pending`. A console that called a
    desired-but-unloaded model "healthy" would be reporting an intention as an outcome, which is the
    exact failure conflict detection exists to catch — and here it would be self-inflicted.

  * A GPU modality that is not resident **because another tenant holds the GPU** is `stopped`, not
    `failed`. On-demand loading is the design (Principle II): the model is not broken, it is not
    loaded, and those call for completely different reactions. Labelling it a failure would
    misrepresent the platform's core resource policy as a fault and send an operator debugging
    something that is working exactly as intended.

  * A CPU modality is `healthy` whenever its child answers — it is off-lease and has no resident
    question to ask.
"""

#: `Endpoint.status`, data-model §7 — the complete vocabulary (FR-415).
STATUSES = ("unconfigured", "pending", "starting", "healthy", "degraded", "draining", "stopped",
            "failed", "unknown")

#: Modalities served off-lease on the CPU. They have no residency question: if the child answers,
#: the endpoint is up.
CPU_MODALITIES = {"embedding", "tabular"}

#: Agent engine states that mean the child is up and serving.
_SERVING_STATES = {"ready", "cold", "idle"}


def endpoint(*, modality, desired=None, engine=None, activation=None, job_holds_gpu=False,
             agent_reachable=True):
    """One synthesized `Endpoint`.

    `desired` is the registry's serving pointer, `engine` the agent's report for the engine that
    would serve it, `activation` the in-flight activation record if any.
    """
    desired = desired or {}
    engine = engine or {}

    resident_identity = engine.get("model_identity")
    resident = {
        # The agent-reported LOADED identity (022), never the registry's desired pointer. The two
        # legitimately diverge during an in-flight activation — which is exactly when an operator is
        # looking — and sourcing this from the pointer would manufacture agreement that does not
        # exist.
        "modelIdentity": resident_identity,
        "registryVersion": engine.get("registry_version"),
        "engineId": engine.get("engine_id"),
        "host": engine.get("host") or ("local" if engine else None),
    }

    status = _status(modality=modality, desired=desired, engine=engine, activation=activation,
                     job_holds_gpu=job_holds_gpu, agent_reachable=agent_reachable)

    return {
        "id": f"{modality}:{desired.get('modelName') or engine.get('engine_id') or 'unconfigured'}",
        "modality": modality,
        "desired": {
            "modelName": desired.get("modelName"),
            "version": desired.get("version"),
            "alias": desired.get("alias"),
            "activationState": (activation or {}).get("state"),
        },
        "resident": resident,
        "status": status,
        # Reported as `null` rather than zeros: this platform records per-request metrics in
        # Prometheus, not per endpoint over a window, and a zeroed traffic block would read as "no
        # traffic" instead of "not measured here".
        "traffic": None,
        "conflict": _conflict(desired, resident, status),
        "lastUpdated": None,
    }


def _status(*, modality, desired, engine, activation, job_holds_gpu, agent_reachable):
    if not desired.get("modelName") and not engine:
        return "unconfigured"

    if not agent_reachable:
        # We cannot see residency, and residency is what `healthy` requires. `unknown` — never
        # `failed`, and never `healthy` on the strength of the desired pointer alone.
        return "unknown"

    engine_state = str(engine.get("state") or "").lower()
    residency = str(engine.get("residency_state") or "").lower()

    if residency == "draining":
        return "draining"
    if residency in ("loading", "rolling-back") or engine_state == "loading":
        return "starting"
    if (activation or {}).get("state") in ("pending", "running", "in-progress"):
        return "starting"

    if engine_state in ("wedged", "error", "crashed", "failed"):
        return "failed"

    if modality in CPU_MODALITIES:
        # Off-lease: no residency question to ask. If the child answers, it is up.
        return "healthy" if engine_state in _SERVING_STATES else "stopped"

    resident = bool(engine.get("model_identity")) and residency in ("", "resident")
    if resident:
        # `degraded` when what is loaded is not what was asked for — the endpoint is serving, but
        # not the thing the operator believes it is serving.
        if desired.get("version") and engine.get("registry_version") \
                and str(desired["version"]) != str(engine["registry_version"]):
            return "degraded"
        return "healthy"

    if job_holds_gpu:
        # `stopped`, NOT `failed`. On-demand loading is the design; the model is not broken, it is
        # not loaded, and calling that a failure sends an operator to debug a working system.
        return "stopped"

    # Desired but not resident, with nothing else explaining it: the load has not happened yet.
    return "pending" if desired.get("modelName") else "stopped"


def _conflict(desired, resident, status):
    """Desired vs resident, disclosed rather than reconciled.

    Only raised when both sides have a claim. A desired pointer with nothing resident is `pending`,
    not a disagreement — nobody is asserting two different things.
    """
    if status in ("unknown", "unconfigured"):
        return None
    desired_version = desired.get("version")
    resident_version = resident.get("registryVersion")
    if not desired_version or not resident_version:
        return None
    if str(desired_version) == str(resident_version):
        return None
    return {
        "entity": "endpoint",
        "entityId": desired.get("modelName"),
        "conflict": True,
        "skewExceeded": False,
        "sources": [
            {"source": "registry", "state": f"desired v{desired_version}", "observedAt": None},
            {"source": "agent", "state": f"resident v{resident_version}", "observedAt": None},
        ],
        "suggestedAction": "inspect-journal",
    }
