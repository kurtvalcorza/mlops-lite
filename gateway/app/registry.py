"""MLflow model-registry client (T019, US2).

Wraps the MLflow registry so the gateway can register model versions, list/compare
them, and promote one to the **serving** position. Promotion uses MLflow *aliases*
(the modern, non-deprecated mechanism) rather than the legacy stage transitions, so a
single named alias (`serving`) always points at the version US1 should resolve.

The MLflow server version is pinned in infra/mlflow (3.14.0, 007 US1); this client matches it.
"""
import os
import time
from typing import Optional

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from platformlib import llmresolve

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
SERVING_ALIAS = "serving"

# 022: the configured default base — what serves when no serving-LLM was ever selected (the
# ActiveServingLLM pointer's documented unset behavior, data-model.md).
DEFAULT_LLM = os.getenv("SERVING_MODEL", "qwen2.5-7b-instruct-q4_k_m")

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


def promote(name: str, version: str, override: bool = False) -> dict:
    """Point the `serving` alias at `version` (FR-006), **gated** (011 FR-105).

    This is the single promotion choke-point — every alias move runs through the evaluation gate, so
    there is no ungated back-door (SC-066). The gate compares the candidate's logged eval metric
    against the incumbent's (a metric lookup — it loads no model) and returns a verdict: in the default
    hard-gate mode a `blocked` verdict refuses the move (the alias stays put) unless `override` is set;
    a `pass`/`warn` verdict (or an override) moves the alias, with `warn` flagged. The verdict (and
    whether the alias moved) is always returned for the API + UI to surface.

    Imported lazily to avoid an import cycle (evaluation imports this module).
    """
    from . import evaluation

    c = _client()
    verdict = evaluation.gate(name, version, override=override, client=c)
    if verdict["verdict"] == "blocked":
        return {"name": name, "serving_version": _serving_version(c, name), "alias": SERVING_ALIAS,
                "promoted": False, "verdict": verdict}
    try:
        c.set_registered_model_alias(name, SERVING_ALIAS, version)
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    return {"name": name, "serving_version": str(version), "alias": SERVING_ALIAS,
            "promoted": True, "verdict": verdict}


# --- 022 US1/US4: the active serving-LLM pointer + LLM target resolution ---------------------------
#
# The pointer (data-model.md §ActiveServingLLM) lives in the relational store (T461), NOT in an
# MLflow alias — it spans model NAMES (base vs fine-tune), which a per-model alias cannot express
# (research R1). Reads are best-effort (unset/unreachable ⇒ the configured default base — the
# documented unset behavior); the WRITE fails loud (RegistryError) so a promote whose go-live
# pointer didn't stick is surfaced, never silent.


def _store():
    from platformlib import store
    return store


def get_serving_llm() -> Optional[dict]:
    """The active serving-LLM selection {model_name, selected_at, selected_by}, or None when unset
    (or the store is unreachable — both read as 'the configured default base serves')."""
    try:
        s = _store()
        conn = s.connect()
        try:
            return s.get_serving_llm(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort read; unset ⇒ default base
        return None


def set_serving_llm(model_name: str, actor: str = "operator") -> dict:
    """Record `model_name` as the active serving LLM (022 US1 — the gated promote IS the go-live
    action; this is its persistence half, the agent reload is the other). Fail-loud."""
    try:
        s = _store()
        conn = s.connect()
        try:
            # 023 US4: bootstrap() VERIFIES schema compatibility now (gateway startup applies
            # the migrations, FR-299). The guard survives so a promote against an unmigrated or
            # newer-schema DB fails HERE with the migration verdict — before the pointer write
            # half-applies (the original Codex F2 concern, now enforced by the ledger).
            s.bootstrap(conn)
            s.set_serving_llm(conn, model_name, time.time(), actor)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — normalize driver/connection errors
        raise RegistryError(f"active serving-LLM pointer write failed: {e}") from e
    return {"model_name": model_name, "selected_by": actor}


def restore_serving_llm(prior: Optional[dict]) -> None:
    """Roll the active serving-LLM pointer back to a previously-captured value (022 FR-265). Called
    when a promote's reload comes back `unresolvable`: the alias moved but the agent can't load the
    artifact, so the pointer must NOT stay pointed at it — a persisted EXPLICIT pointer to an
    unloadable model 503s the next cold load. `prior` is a `get_serving_llm()` snapshot taken BEFORE
    the promote overwrote it: a concrete model ⇒ restore it; None (was unset) ⇒ clear it, back to
    best-effort adoption / the configured default, which never bricks serving. Fail-loud."""
    try:
        s = _store()
        conn = s.connect()
        try:
            if prior and prior.get("model_name"):
                s.set_serving_llm(conn, prior["model_name"],
                                  prior.get("selected_at") or time.time(),
                                  prior.get("selected_by") or "operator")
            else:
                s.clear_serving_llm(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — normalize driver/connection errors
        raise RegistryError(f"active serving-LLM pointer rollback failed: {e}") from e


def active_serving_llm_name() -> str:
    """The model name that SHOULD be serving text-generation (FR-276 — the disambiguator when
    several LLM models hold a promoted alias): the explicit pointer, else the single promoted
    `@serving` text-generation model ADOPTED (F4 — so an upgraded install with a pre-022 promoted
    fine-tune and no pointer shows/serves THAT model, matching the agent, not a stale default), else
    the configured default base. The agent's `serving_llm.resolve` mirrors this exactly.

    The store is read DIRECTLY here (not via the exception-swallowing `get_serving_llm`) so a store
    OUTAGE returns the default — matching the agent, whose `active_model_name` raises
    ResolutionUnavailable on the same outage and falls back to the env default — rather than being
    mistaken for an unset pointer and ADOPTING a promoted model the agent isn't serving (that
    mismatch would recreate the console↔agent divergence F4 removes)."""
    s = _store()
    try:
        conn = s.connect()
        try:
            rec = s.get_serving_llm(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — store unreachable ⇒ the configured default (agent does the same)
        return DEFAULT_LLM
    if rec:
        return rec["model_name"]
    try:
        return llmresolve.adopt_active_llm(_client()) or DEFAULT_LLM  # unset ⇒ adopt the sole LLM
    except Exception:  # noqa: BLE001 — best-effort adoption; registry blip ⇒ the default base
        return DEFAULT_LLM


def llm_target_info(name: str, version: str) -> Optional[dict]:
    """Pre-promotion resolution check for a text-generation version (022 T467/T474, FR-265).

    Returns None when (name, version) is not a text-generation target (task tag or inferred kind
    — T478), else {task, kind, base, error}: `error` is set when the target cannot resolve at the
    REGISTRY level (adapter with no/unresolvable base — the artifact-presence half is the agent's
    probe). The promote route refuses on `error` BEFORE the gate runs, so the alias — and the
    currently-served LLM — stay unchanged."""
    c = _client()
    try:
        mv = c.get_model_version(name, str(version))
    except MlflowException as e:
        raise RegistryError(str(e)) from e
    tags = dict(mv.tags or {})
    task = tags.get(TASK_TAG) or llmresolve.task_from_kind(tags)
    if task != llmresolve.TEXT_GENERATION:
        return None
    kind = llmresolve.effective_kind(tags)
    info = {"task": task, "kind": kind, "base": None, "error": None}
    if kind == llmresolve.KIND_ADAPTER:
        base_ref = tags.get(llmresolve.BASE_MODEL_TAG)
        if not base_ref:
            info["error"] = (f"{name} v{version} is a LoRA adapter with no base_model lineage — "
                             f"its base cannot be resolved (FR-264/265)")
            return info
        try:
            bmv = llmresolve.resolve_base_version(c, base_ref)
            info["base"] = {"name": bmv.name, "version": str(bmv.version), "source": bmv.source}
        except llmresolve.LLMResolutionError as e:
            info["error"] = str(e)
        except MlflowException as e:
            raise RegistryError(str(e)) from e
    return info


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
        safe_base = str(base).replace("'", "''")  # escape like resolve_serving_target (Claude review)
        for bmv in sorted(c.search_model_versions(f"name='{safe_base}'"),
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
    if first is None and task == llmresolve.TEXT_GENERATION:
        # 022 T478 (FR-267) defense-in-depth: a legacy promoted LLM version with NO task tag
        # (kind/format only, pre-backfill) is invisible to the tag search above — infer its task
        # from the artifact kind so no valid LLM version is stranded as unroutable.
        try:
            models = c.search_registered_models()
        except MlflowException as e:
            raise RegistryError(str(e)) from e
        for m in models:
            sv = (dict(m.aliases or {})).get(SERVING_ALIAS)
            if not sv:
                continue
            try:
                mv = c.get_model_version(m.name, sv)
            except MlflowException:
                continue
            tags = dict(mv.tags or {})
            if tags.get(TASK_TAG) or llmresolve.task_from_kind(tags) != task:
                continue  # tagged versions were already considered by the search above
            entry = {"name": m.name, "version": str(sv), "task": task,
                     "serving_engine": _resolve_engine(c, mv), "source": mv.source}
            if prefer_name is not None and m.name == prefer_name:
                return entry
            if first is None:
                first = entry
    return first


#: The lineage tags surfaced per LLM serving target (FR-268: base / parent / dataset provenance).
_LINEAGE_KEYS = ("base_model", "dataset_name", "dataset_version", "parent_version", "lineage")


def list_tasks() -> list:
    """Distinct serving tasks discovered from the registry — one entry per registered model's
    `@serving` version, reporting its `task`/`serving_engine` (FR-077). The Infer tab renders one
    panel per task; a serving version with no `task` tag (legacy, pre-009) is reported as task=None
    so the UI degrades it to a read-only "no renderer" placeholder rather than breaking the tab.

    022 (T478/T479): a text-generation version's task is INFERRED from its artifact kind when the
    tag is missing (legacy fine-tunes render a real LLM panel, not "no renderer" — FR-267), each
    LLM entry carries its base-vs-adapter `kind` + lineage (FR-268), and the text-generation rows
    are filtered to the ACTIVE serving-LLM pointer (FR-276): a promote moves only its own model's
    `@serving` alias, so several LLM models can each retain one — but only the active-pointer
    model is the live target; a previously-active LLM must not linger as a stale duplicate."""
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
        task = tags.get(TASK_TAG) or llmresolve.task_from_kind(tags)
        entry = {
            "model": m.name,
            "version": str(sv),
            "task": task,
            "serving_engine": _resolve_engine(c, mv),
        }
        if task == llmresolve.TEXT_GENERATION:
            entry["kind"] = llmresolve.effective_kind(tags)
            lineage = {k: tags[k] for k in _LINEAGE_KEYS if tags.get(k)}
            entry["lineage"] = lineage or None
        out.append(entry)
    text_gen = [e for e in out if e["task"] == llmresolve.TEXT_GENERATION]
    if text_gen:
        if len(text_gen) == 1:
            # The lone promoted LLM IS the live text-generation target (adoption makes it the active
            # model when the pointer is unset; an explicit pointer elsewhere is a rare misconfig for
            # which showing the sole LLM beats blanking the Infer tab). Keep it WITHOUT a second
            # registry scan — active_serving_llm_name() would re-walk the registry for a no-op here.
            keep = text_gen[0]
        else:
            # Several promoted LLMs → keep only the ACTIVE one (FR-276). If none is active (an
            # ambiguous unset pointer ⇒ the default, or a pointer to a model with no @serving row),
            # advertise NO live LLM rather than an arbitrary/nondeterministic `text_gen[0]` that
            # contradicts what the agent serves — the operator promotes one to disambiguate.
            active = active_serving_llm_name()
            keep = next((e for e in text_gen if e["model"] == active), None)
        out = [e for e in out if e["task"] != llmresolve.TEXT_GENERATION or e is keep]
    return out
