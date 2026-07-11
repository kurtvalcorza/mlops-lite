"""Cold-load resolver for the active serving-LLM (022 T463 — contracts/serving-resolution.md).

Given the platform's ActiveServingLLM pointer (platformlib.store, written by the gateway's gated
promote), resolve WHAT the llama.cpp engine should serve:

    {model_name, version, kind, base_gguf, adapter_gguf|None, base|None}

    full-model    → base_gguf = the version's source, no adapter
    lora-adapter  → adapter_gguf = the version's source; base_gguf = the registered base's source,
                    resolved from `base_model` lineage in ONE hop (platformlib.llmresolve)

Called by the llama adapter on each cold load (`rebind`) — the exact placement the vision/embed/
tabular children use ("resolve @serving each cold load", research R3), so a promotion is picked up
on the next controlled (re)load with no process restart.

Failure vocabulary (the seam the adapter + reload verb key on):
  - ResolutionError      — the pointer names a target that does NOT resolve (no @serving version,
                           no base, adapter chain, artifact missing). The promote/select is refused
                           and the currently-served LLM stays unchanged (FR-265). Fail loud.
  - ResolutionUnavailable — the pointer store or the registry is UNREACHABLE. The adapter falls
                           back to the env-configured default base (today's behavior) and surfaces
                           the degradation in health — the served identity still reports what is
                           ACTUALLY bound, so honesty (FR-260) is preserved; nothing guesses.

Artifact locality (research R7): a filesystem source must already exist — nothing is ever
downloaded from outside the box. An `s3://` source (adapters live in the models bucket of the
LOCAL Garage) is materialized once into the state-dir cache; that is same-machine object storage,
so Principle I holds. Stdlib-importable: mlflow/boto3/psycopg load lazily inside resolve() — the
agent's default transport carries no pip deps; the training venv it runs under has all three.
"""
import hashlib
import os

from platformlib import llmresolve
from platformlib.topology import STATE_DIR


class ResolutionError(Exception):
    """The active target is invalid — refuse the promote/select, served LLM unchanged (FR-265)."""


class ResolutionUnavailable(Exception):
    """Pointer store / registry unreachable — the caller may fall back to the configured default
    (surfaced, never silent)."""


#: Where s3:// adapter artifacts are materialized (keyed by source digest — a re-promote of the
#: same version reuses the cached file; a new version is a new source → a new cache entry).
CACHE_DIR = os.path.join(STATE_DIR, "llm-artifacts")


def active_model_name(store=None):
    """The pointer's model name, or None when unset (⇒ the configured default base serves).
    `store` is injectable for tests (defaults to platformlib.store)."""
    if store is None:
        from platformlib import store as _default_store
        store = _default_store
    try:
        conn = store.connect()
        try:
            rec = store.get_serving_llm(conn)
        finally:
            conn.close()
    except store.StoreError as e:
        raise ResolutionUnavailable(f"serving-LLM pointer unreadable: {e}") from e
    return rec["model_name"] if rec else None


def _default_client():
    try:
        from mlflow.tracking import MlflowClient
        return MlflowClient(tracking_uri=os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500"))
    except Exception as e:  # noqa: BLE001 — no mlflow in this interpreter
        raise ResolutionUnavailable(f"mlflow client unavailable: {e}") from e


def resolve(*, store=None, client=None, materialize=None):
    """The full cold-load resolution → {model_name, version, kind, base_gguf, adapter_gguf, base},
    or None when the configured env default should serve. Raises ResolutionError (invalid EXPLICIT
    selection, refuse — FR-265) / ResolutionUnavailable (infra down, env fallback).

    Selection mirrors gateway `registry.active_serving_llm_name` (F4): an explicit pointer wins and
    resolves FAIL-LOUD; when the pointer is unset the single promoted `@serving` text-generation
    model is ADOPTED so the agent serves what the console shows and a pre-022 promotion keeps
    serving. Adoption is BEST-EFFORT — an adopted model that won't resolve (missing base/artifact)
    falls back to the env default (return None), never bricking serving into an outage."""
    name = active_model_name(store)
    adopted = name is None  # unset pointer → adopt-best-effort; a set pointer is fail-loud
    if client is None:
        try:
            client = _default_client()
        except ResolutionUnavailable:
            if adopted:
                return None  # can't reach the registry to find a model to adopt → env default
            raise
    if adopted:
        name = llmresolve.adopt_active_llm(client)
        if name is None:
            return None  # nothing promoted to adopt → the configured env default serves
    try:
        rec = llmresolve.resolve_serving_record(client, name)
        mat = materialize or _materialize
        if rec["kind"] == llmresolve.KIND_ADAPTER:
            base_gguf = mat(rec["base"]["source"])
            adapter_gguf = mat(rec["source"])
        else:
            base_gguf = mat(rec["source"])
            adapter_gguf = None
    except (llmresolve.LLMResolutionError, ResolutionError) as e:
        if adopted:
            return None  # an adopted model that won't resolve → env default, never an outage
        raise ResolutionError(str(e)) from e
    except ResolutionUnavailable:
        raise  # store/registry unreachable mid-resolve — propagate (env fallback at the adapter)
    except Exception as e:  # noqa: BLE001 — registry down/unreachable, not an invalid target
        raise ResolutionUnavailable(f"registry unreachable resolving {name!r}: {e}") from e
    return {"model_name": rec["model_name"], "version": rec["version"], "kind": rec["kind"],
            "base_gguf": base_gguf, "adapter_gguf": adapter_gguf, "base": rec["base"]}


def _materialize(source: str) -> str:
    """A local file path for `source`. Filesystem sources must already exist (the operator-curated
    zoo — FR-265, nothing auto-downloaded); s3:// sources fetch once from the local Garage."""
    if str(source).startswith("s3://"):
        return _fetch_s3(str(source))
    path = os.path.expanduser(str(source))
    if not os.path.isfile(path):
        raise ResolutionError(
            f"artifact not found (or not a file) at {path} — the base/adapter GGUF must already "
            f"be present locally (nothing is auto-downloaded, FR-265)")
    return path


def _fetch_s3(source: str) -> str:
    bucket, _, key = source[len("s3://"):].partition("/")
    if not bucket or not key:
        raise ResolutionError(f"malformed artifact source {source!r}")
    cache = os.path.join(os.path.expanduser(CACHE_DIR),
                         hashlib.sha256(source.encode()).hexdigest()[:16] + ".gguf")
    if os.path.isfile(cache) and os.path.getsize(cache) > 0:
        return cache
    try:
        from platformlib.store import s3_client
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".part"
        s3_client().download_file(bucket, key, tmp)
        os.replace(tmp, cache)
    except ResolutionError:
        raise
    except Exception as e:  # noqa: BLE001 — classify: object gone = invalid target; else outage
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) \
            if hasattr(e, "response") else ""
        if code in ("404", "NoSuchKey") or "Not Found" in str(e) or "NoSuchKey" in str(e):
            raise ResolutionError(f"artifact object missing in the store: {source}") from e
        raise ResolutionUnavailable(f"object store unreachable fetching {source}: {e}") from e
    return cache
