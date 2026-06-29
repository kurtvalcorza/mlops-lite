"""MLflow model-registry client (T019, US2).

Wraps the MLflow registry so the gateway can register model versions, list/compare
them, and promote one to the **serving** position. Promotion uses MLflow *aliases*
(the modern, non-deprecated mechanism) rather than the legacy stage transitions, so a
single named alias (`serving`) always points at the version US1 should resolve.

The MLflow server version is pinned in infra/mlflow (3.14.0, 007 US1); this client matches it.
"""
import os
from typing import Optional

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
SERVING_ALIAS = "serving"

# 009 US1 — registry routing metadata (FR-074). Every version SHOULD carry these version tags; the
# gateway resolves "the serving model+engine for task X" from them rather than a hard-coded
# endpoint→model map (FR-075). Versions registered before 009 lack them and are tolerated (task=None).
TASK_TAG = "task"                # text-generation | image-classification | embedding | asr | tabular
ENGINE_TAG = "serving_engine"    # llama.cpp | bentoml | whisper.cpp
BASE_MODEL_TAG = "base_model"    # set on a LoRA adapter version → it inherits the base's engine (FR-076)


class RegistryError(Exception):
    """A registry operation failed (server unreachable or rejected the request)."""


def _client() -> MlflowClient:
    return MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)


def register_model(
    name: str,
    source: str,
    run_id: Optional[str] = None,
    tags: Optional[dict] = None,
) -> dict:
    """Create the registered model (idempotent) and add a new version pointing at `source`."""
    c = _client()
    try:
        try:
            c.create_registered_model(name)
        except MlflowException:
            pass  # already exists — adding another version is fine
        mv = c.create_model_version(name=name, source=source, run_id=run_id, tags=tags or {})
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    return {"name": mv.name, "version": str(mv.version), "source": mv.source, "status": mv.status}


def _serving_version(c: MlflowClient, name: str) -> Optional[str]:
    try:
        return str(c.get_model_version_by_alias(name, SERVING_ALIAS).version)
    except MlflowException:
        return None


def list_versions(name: str) -> list:
    """All versions of one model, newest first, flagged with which one is serving."""
    c = _client()
    try:
        versions = c.search_model_versions(f"name='{name}'")
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    serving = _serving_version(c, name)
    out = [
        {
            "name": mv.name,
            "version": str(mv.version),
            "source": mv.source,
            "run_id": mv.run_id,
            "tags": dict(mv.tags or {}),
            "serving": str(mv.version) == serving,
        }
        for mv in versions
    ]
    out.sort(key=lambda v: int(v["version"]), reverse=True)
    return out


def list_models() -> list:
    """All registered models with their current serving version (if any)."""
    c = _client()
    try:
        models = c.search_registered_models()
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    return [
        {"name": m.name, "serving_version": (dict(m.aliases or {})).get(SERVING_ALIAS)}
        for m in models
    ]


def get_serving(name: str) -> Optional[dict]:
    """The version currently promoted to serving, or None if nothing is promoted."""
    c = _client()
    v = _serving_version(c, name)
    return {"name": name, "version": v, "alias": SERVING_ALIAS} if v else None


def promote(name: str, version: str) -> dict:
    """Point the `serving` alias at `version` (FR-006). Idempotent; overwrites any prior target."""
    c = _client()
    try:
        c.set_registered_model_alias(name, SERVING_ALIAS, version)
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    return {"name": name, "serving_version": str(version), "alias": SERVING_ALIAS}


# --- 009 US1: task/serving-engine routing metadata ------------------------------------------------

def set_version_tags(name: str, version: str, tags: dict) -> dict:
    """Set/overwrite version tags (e.g. task + serving_engine) on an existing version (FR-074).

    Mutable post-registration via `set_model_version_tag`, so the serving LLM/vision can be retagged
    in place without re-registering.
    """
    c = _client()
    try:
        for k, v in (tags or {}).items():
            c.set_model_version_tag(name, str(version), k, str(v))
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    return {"name": name, "version": str(version), "tags": dict(tags or {})}


def _resolve_engine(c: MlflowClient, mv) -> Optional[str]:
    """The serving engine for a version: its own `serving_engine` tag, else (for a LoRA adapter
    carrying a `base_model` tag) the base model's engine (FR-076) — adapters are served on the base's
    path and never need separate engine wiring.

    The trainer tags an adapter's `base_model` with the **raw HF/local base string** (e.g.
    `Qwen/Qwen2.5-0.5B-Instruct`), not a registered model name, so the `@serving`-alias lookup misses
    for real trained adapters (Codex review). Resolve robustly: (1) the base's `@serving` engine, then
    (2) the newest registered version of a model literally named `base`, then (3) — since every LoRA
    adapter in this platform is converted to GGUF and served by llama.cpp — default a tagged adapter
    to `llama.cpp` rather than returning None and dropping it from routing."""
    tags = dict(mv.tags or {})
    engine = tags.get(ENGINE_TAG)
    if engine:
        return engine
    base = tags.get(BASE_MODEL_TAG)
    if not base:
        return None
    try:  # (1) base is a registered model with a @serving alias (the FR-076 happy path)
        bmv = c.get_model_version_by_alias(base, SERVING_ALIAS)
        eng = dict(bmv.tags or {}).get(ENGINE_TAG)
        if eng:
            return eng
    except MlflowException:
        pass
    try:  # (2) base is a registered model without an alias → its newest version's engine
        for bmv in sorted(c.search_model_versions(f"name='{base}'"),
                          key=lambda m: int(m.version), reverse=True):
            eng = dict(bmv.tags or {}).get(ENGINE_TAG)
            if eng:
                return eng
    except MlflowException:
        pass
    # (3) raw HF/local base string (the trainer's actual behavior) → a LoRA adapter is GGUF/llama.cpp.
    return "llama.cpp"


def resolve_serving_target(task: str, prefer_name: Optional[str] = None) -> Optional[dict]:
    """The model+engine currently serving `task`, resolved from registry metadata (FR-075).

    Finds versions tagged `task=<task>` (`search_model_versions("tags.task='…'")`) and returns the
    one its model has promoted to the `@serving` alias — so routing follows the alias + task tag, not
    a hard-coded endpoint→model map. Returns None when no serving version carries the task (e.g. the
    modality isn't seeded yet), which callers surface as "not configured".

    `prefer_name` disambiguates when several models share a task (Codex review): the gateway's /infer
    always proxies to the single llama supervisor configured by SERVING_MODEL, so a second promoted
    text-generation model (e.g. a test/LoRA model) must NOT be reported as the served version. When
    `prefer_name`'s own `@serving` version carries the task it wins; otherwise the first match is used.
    """
    c = _client()
    # Single-quote-escape the task so a value can't break out of the filter string.
    safe = str(task).replace("'", "''")
    try:
        versions = c.search_model_versions(f"tags.{TASK_TAG}='{safe}'")
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    first = None
    for mv in versions:
        if _serving_version(c, mv.name) != str(mv.version):  # only the promoted version serves
            continue
        entry = {
            "name": mv.name,
            "version": str(mv.version),
            "task": task,
            "serving_engine": _resolve_engine(c, mv),
            "source": mv.source,
        }
        if prefer_name is not None and mv.name == prefer_name:
            return entry  # the configured backend model wins over any other task-mate
        if first is None:
            first = entry
    return first


def list_tasks() -> list:
    """Distinct serving tasks discovered from the registry — one entry per registered model's
    `@serving` version, reporting its `task`/`serving_engine` (FR-077). The Infer tab renders one
    panel per task; a serving version with no `task` tag (legacy, pre-009) is reported as task=None
    so the UI degrades it to a read-only "no renderer" placeholder rather than breaking the tab.
    """
    c = _client()
    try:
        models = c.search_registered_models()
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    out = []
    for m in models:
        sv = (dict(m.aliases or {})).get(SERVING_ALIAS)
        if not sv:
            continue  # nothing promoted → no panel for this model
        try:
            mv = c.get_model_version(m.name, sv)
        except MlflowException:
            continue
        tags = dict(mv.tags or {})
        out.append({
            "model": m.name,
            "version": str(sv),
            "task": tags.get(TASK_TAG),
            "serving_engine": _resolve_engine(c, mv),
        })
    return out
