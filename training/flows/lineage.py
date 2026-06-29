"""Shared fine-tune lineage + adapter-chaining helper (010 US4, T195/T196 — FR-095/FR-096).

Every fine-tune flow (vision / embeddings / ASR **and** the existing LLM LoRA) routes its provenance
tags through here so genealogy is recorded identically across modalities — the precondition for the
gated-promotion reasoning in 011 ("is this newer than the serving version?"). Two responsibilities:

1. **Lineage tags** (`lineage_tags`): a single dict carrying `base_model`, `dataset_name`,
   `dataset_version`, and a `lineage` marker (`base` for a stock-base run, `chained` for a run that
   resumed from a prior registered version). A chained run also records `parent_version` and links
   the child MLflow run to the parent run (`mlflow.note` + a `parent_run_id` tag).

2. **Parent resolution** (`resolve_parent`): for a chained run, look up the requested parent version of
   the output model, **reject a parent whose `task` differs from the requested modality** (no
   cross-modality chaining — FR-096), and return the metadata a flow needs to warm-start from it
   (`source`, `run_id`, `task`). The actual trainable load (`PeftModel.from_pretrained(...,
   is_trainable=True)` for adapter modalities, prior `state_dict`/checkpoint for full-weight ones) is
   the flow's job; this helper only resolves + validates the parent.

Stdlib + mlflow only (no torch here) so it stays a light import shared by all flows.
"""
import os
from typing import Optional

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")

# Mirror the gateway registry's tag vocabulary (gateway/app/registry.py) so a fine-tuned version is
# routed by the same metadata the serving side reads.
TASK_TAG = "task"
LINEAGE_TAG = "lineage"            # "base" (stock base) | "chained" (resumed from a prior version)
PARENT_VERSION_TAG = "parent_version"
PARENT_RUN_TAG = "parent_run_id"


class LineageError(Exception):
    """A chaining request is invalid (parent missing or a cross-modality parent — FR-096)."""


def _client():
    from mlflow.tracking import MlflowClient
    return MlflowClient(tracking_uri=MLFLOW_URI)


def lineage_tags(base_model: str, dataset_name: str, dataset_version: str,
                 parent_version: Optional[str] = None,
                 parent_run_id: Optional[str] = None) -> dict:
    """The provenance tags every fine-tune stamps on its registered version (FR-095).

    A stock-base run is `lineage=base`; a run that resumed from a prior registered version is
    `lineage=chained` and additionally records `parent_version` + the parent's MLflow run id (so the
    registry reads as an explicit "trained-on-top-of vN" genealogy, not a flat list).
    """
    tags = {
        "base_model": str(base_model),
        "dataset_name": str(dataset_name),
        "dataset_version": str(dataset_version),
        LINEAGE_TAG: "chained" if parent_version else "base",
    }
    if parent_version:
        tags[PARENT_VERSION_TAG] = str(parent_version)
    if parent_run_id:
        tags[PARENT_RUN_TAG] = str(parent_run_id)
    return tags


def resolve_parent(output_name: str, parent_version: str, expected_task: str) -> dict:
    """Resolve + validate a chaining parent before training (FR-096).

    Looks up `output_name` version `parent_version`, **rejects a parent whose `task` differs from the
    requested modality** (`expected_task`) so a vision run can't resume from an ASR adapter, and returns
    `{name, version, source, run_id, task}` for the flow to warm-start from. Raises `LineageError` if
    the parent doesn't exist or the modality mismatches.
    """
    from mlflow.exceptions import MlflowException

    c = _client()
    try:
        mv = c.get_model_version(output_name, str(parent_version))
    except MlflowException as e:
        raise LineageError(
            f"chaining parent {output_name} v{parent_version} not found: {e}") from e
    tags = dict(mv.tags or {})
    parent_task = tags.get(TASK_TAG)
    # No cross-modality chaining: the parent must be the same task as the requested fine-tune. A parent
    # with no `task` tag (legacy/pre-009) can't be proven compatible, so it is rejected too.
    if parent_task != expected_task:
        raise LineageError(
            f"cross-modality chaining rejected: parent {output_name} v{parent_version} has "
            f"task={parent_task!r}, but the requested fine-tune is task={expected_task!r}")
    return {
        "name": mv.name,
        "version": str(mv.version),
        "source": mv.source,
        "run_id": mv.run_id,
        "task": parent_task,
    }


def link_parent_run(parent_run_id: Optional[str]) -> None:
    """Link the *active* MLflow run to its parent run (call inside `with mlflow.start_run()`).

    Sets the standard `mlflow.parentRunId` tag so MLflow's UI nests the child under the parent, plus a
    human-readable note — making the chain navigable, not just tag-encoded (FR-095). No-op for a
    stock-base run (`parent_run_id is None`).
    """
    if not parent_run_id:
        return
    import mlflow
    mlflow.set_tag("mlflow.parentRunId", str(parent_run_id))
    mlflow.set_tag(PARENT_RUN_TAG, str(parent_run_id))
