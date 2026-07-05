"""Shared serving-LLM registry walk (022 T463/T464 — contracts/serving-resolution.md).

ONE implementation of "active model name → its `@serving` version → the base/adapter record",
used by BOTH resolution sites so they cannot drift:

  - the host agent's cold-load resolver (`hostagent/serving_llm.py`) — adds the pointer read and
    artifact materialization on top of this walk;
  - the gateway's promote pre-check + task surfaces (`gateway/app/registry.py`) — refuses an
    unresolvable promotion BEFORE the alias moves (FR-265) and exposes base-vs-adapter kind.

They can't share an import path any other way: the gateway image ships only `gateway/app` +
`platformlib` (gateway/Dockerfile), and the agent's default transport is stdlib-only — so this
module is dependency-free and takes a duck-typed MLflow client (`get_model_version_by_alias`,
`search_model_versions`) rather than importing mlflow. Client/connection exceptions PROPAGATE
(the callers classify unreachable-vs-invalid); only "does not exist" is normalized here.

Kind rules (data-model.md §LLMVersion):
  - `kind=full-model` → served standalone (`-m source`).
  - `kind=lora-adapter` → served on its base (`-m base --lora source`); `base_model` MUST resolve
    DIRECTLY to a registered full-model version — an adapter pointing at another adapter is a
    resolution error, not a multi-hop walk (spec review PR #64, R2 note).
  - Legacy versions without `kind` (pre-022, FR-267) are inferred: a `base_model` + `format=gguf`
    pair is an adapter (the fine-tune flow's historical shape); anything else standalone.
"""

SERVING_ALIAS = "serving"
TASK_TAG = "task"
ENGINE_TAG = "serving_engine"
KIND_TAG = "kind"
BASE_MODEL_TAG = "base_model"
BASE_ID_TAG = "base_id"          # a registered BASE's raw HF/local id (scripts/register_base_gguf)
FORMAT_TAG = "format"

KIND_FULL = "full-model"
KIND_ADAPTER = "lora-adapter"
TEXT_GENERATION = "text-generation"
LLAMA_ENGINE = "llama.cpp"


class LLMResolutionError(Exception):
    """The target does not resolve (no @serving / no base / adapter chain) — refuse the
    promote/select with this reason and leave the served LLM unchanged (FR-265)."""


def _escape(value: str) -> str:
    """Single-quote-escape a value for an MLflow filter string (the registry.py house rule)."""
    return str(value).replace("'", "''")


def _is_not_found(exc: Exception) -> bool:
    """True when an MLflow error means 'does not exist' (vs. unreachable). Duck-typed on the REST
    error code so this module needs no mlflow import."""
    if getattr(exc, "error_code", None) == "RESOURCE_DOES_NOT_EXIST":
        return True
    text = str(exc)
    return "RESOURCE_DOES_NOT_EXIST" in text or "not found" in text.lower()


def _by_alias(client, name: str):
    """The @serving model version of `name`, or None when the model/alias doesn't exist.
    Connection errors propagate for the caller to classify."""
    try:
        return client.get_model_version_by_alias(name, SERVING_ALIAS)
    except Exception as e:  # noqa: BLE001 — only not-found is normalized; the rest propagates
        if _is_not_found(e):
            return None
        raise


def effective_kind(tags: dict) -> str:
    """A version's base-vs-adapter kind, inferring legacy shapes (FR-267): an explicit
    `kind` wins; else `base_model`+`format=gguf` (the pre-022 fine-tune stamp) is an adapter;
    anything else serves standalone."""
    kind = (tags or {}).get(KIND_TAG)
    if kind in (KIND_FULL, KIND_ADAPTER):
        return kind
    if (tags or {}).get(BASE_MODEL_TAG) and (tags or {}).get(FORMAT_TAG) == "gguf":
        return KIND_ADAPTER
    return KIND_FULL


def task_from_kind(tags: dict):
    """Defense-in-depth task inference (022 T478, FR-267): a LoRA adapter or any GGUF artifact is
    a text-generation version even when the `task` tag is missing (legacy, pre-backfill) — GGUF is
    llama.cpp's format, so the inference is unambiguous. Returns the task or None (no guess for
    non-LLM shapes)."""
    tags = tags or {}
    if tags.get(KIND_TAG) == KIND_ADAPTER or tags.get(FORMAT_TAG) == "gguf":
        return TEXT_GENERATION
    return None


def resolve_base_version(client, base_ref: str):
    """Map an adapter's `base_model` reference to a registered full-model version — SINGLE hop
    (contracts/serving-resolution.md). `base_ref` is either a registered model name or the raw
    HF/local id the trainer stamps (matched via the base's `base_id` tag). Raises
    LLMResolutionError when nothing full-model matches (including the adapter-chain case)."""
    candidates = []
    aliased = _by_alias(client, base_ref)
    if aliased is not None:
        candidates.append(aliased)
    named = client.search_model_versions(f"name='{_escape(base_ref)}'")
    candidates += sorted(named, key=lambda m: int(m.version), reverse=True)
    if not candidates:  # not a registered name → the trainer's raw id, matched by base_id tag
        tagged = client.search_model_versions(f"tags.{BASE_ID_TAG}='{_escape(base_ref)}'")
        candidates = sorted(tagged, key=lambda m: int(m.version), reverse=True)
    for mv in candidates:
        if effective_kind(dict(mv.tags or {})) == KIND_FULL:
            return mv
    if candidates:
        raise LLMResolutionError(
            f"base {base_ref!r} resolves only to adapter versions — an adapter's base must be a "
            f"registered full-model version (no adapter chains)")
    raise LLMResolutionError(
        f"base {base_ref!r} is not a registered full-model text-generation version — register the "
        f"local base GGUF first (scripts/register_base_gguf.py)")


def resolve_serving_record(client, model_name: str) -> dict:
    """`model_name` → its @serving version → the servable record:
    {model_name, version, kind, source, tags, base:{name, version, source}|None}.

    Pure registry read — no file checks (the agent layers artifact materialization on top).
    Raises LLMResolutionError when the @serving alias or an adapter's base doesn't resolve."""
    mv = _by_alias(client, model_name)
    if mv is None:
        raise LLMResolutionError(
            f"{model_name!r} has no @{SERVING_ALIAS} version — nothing is promoted to serve")
    tags = dict(mv.tags or {})
    kind = effective_kind(tags)
    base = None
    if kind == KIND_ADAPTER:
        base_ref = tags.get(BASE_MODEL_TAG)
        if not base_ref:
            raise LLMResolutionError(
                f"{model_name} v{mv.version} is a LoRA adapter with no base_model lineage — "
                f"its base cannot be resolved (FR-264/265)")
        base_mv = resolve_base_version(client, base_ref)
        base = {"name": base_mv.name, "version": str(base_mv.version), "source": base_mv.source}
    return {"model_name": mv.name, "version": str(mv.version), "kind": kind,
            "source": mv.source, "tags": tags, "base": base}
