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
