"""Ephemeral batch-inference Prefect flow on the native daemon (014 US1, grill A).

The gateway (Docker) has no GPU, so batch can't run there — it runs **natively** where serving lives,
launched via the gateway `POST /batch`. The flow scores every row of a registered dataset version
through the **existing serving path** and writes a content-addressed result to Garage via the gateway's
`batch.py` core (one implementation of the scoring/write logic, shared with the gateway).

**Lease discipline (Principle II).** GPU-backed models (LLM, vision) are scored through the serving
supervisor / bento — the **single one-model-in-VRAM lease tenant** the online `/infer` path already uses
— so a batch never pins a *second* model in VRAM and an online `/infer` arriving mid-batch is serialized
by the same mutex. CPU-backed (tabular) models score **off-lease**. The model stays resident across the
batch's back-to-back requests (the idle release won't fire mid-job), giving the acquire-once/hold/release
shape the grill described, without the daemon loading a second copy.

**Version assertion (025 US1, FR-348/350, closes SC-068).** Before scoring a GPU-lease modality the
flow asserts the requested `registry_version` is the one the serving tenant actually holds — probing
the engine's agent-reported resident identity (022 FR-260) and, for the reloadable LLM path, making
the active `@serving` target live first. A **confirmed** different resident version is refused
(`BatchVersionMismatch`) rather than silently scored, and the reload never preempts a job/GPU-batch
holder (a refusal surfaces as a clean 409 → refusal, FR-350). Consistent with 015's score-at-
registration decision, the platform does not cold-load an *arbitrary* non-`@serving` version on demand:
a batch pinned to a version that is neither resident nor the promoted `@serving` target is refused
(promote/serve it first), not silently mis-scored. The ordering/assertion logic (`assert_version_resident`)
is transport-free and unit-tested offline; the real load-under-lease leg is validated on hardware (SC-175).

Ephemeral Prefect, exactly like `finetune.py`: if Prefect isn't importable the decorator degrades to a
no-op so the flow still runs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # training/

try:
    from prefect import flow
except Exception:  # Prefect absent → no-op decorator (ephemeral, like finetune.py)
    def flow(fn=None, **_):
        return fn if fn else (lambda f: f)


# 018 T358/T360/T361: the LLM/vision/tabular engines moved from their standalone daemons (:8090 /
# :8092 / :8094, deleted) to the host agent's /engines/<id> sub-paths. This flow runs natively in
# WSL, so the defaults are localhost:8100 (the gateway's injected URLs don't reach the WSL daemon
# env); the /infer, /classify, /predict verb paths are unchanged (FR-177). (The old TABULAR_URL
# default was :8093 — the embed port — a pre-existing mismatch this flip also corrects.)
SERVING_URL = os.getenv("SERVING_URL", "http://localhost:8100/engines/llm")
BENTO_URL = os.getenv("BENTO_URL", "http://localhost:8100/engines/vision")
TABULAR_URL = os.getenv("TABULAR_URL", "http://localhost:8100/engines/tabular")


def _load_batch():
    """The gateway's batch core (score + content-addressed write) via the audited platformlib bridge
    (018 T362.1, FR-176 — replaces a per-seam gateway/ path injection)."""
    from platformlib.gateway_bridge import batch
    return batch()


def _predict_fn(modality: str):
    """A per-row `predict_fn(row)->output` over the existing serving path. GPU modalities go through the
    resident serving tenant (the VRAM lease); tabular scores off-lease."""
    import httpx

    if modality in ("text-generation", "llm"):
        def predict(row):
            prompt = row.get("instruction") or row.get("prompt") or row.get("input") or ""
            r = httpx.post(f"{SERVING_URL}/infer",
                           json={"prompt": prompt, "max_tokens": int(row.get("max_tokens", 64)),
                                 "temperature": 0.0}, timeout=300)
            r.raise_for_status()
            return r.json().get("text", "")
        return predict
    if modality in ("image-classification", "vision"):
        import base64

        def predict(row):
            raw = base64.b64decode(row["image_b64"], validate=True)
            r = httpx.post(f"{BENTO_URL}/classify",
                           files={"image": ("image.png", raw, "image/png")}, timeout=120)
            r.raise_for_status()
            data = r.json()
            preds = data.get("predictions") or data.get("labels") or []
            return preds[0].get("label") if preds and isinstance(preds[0], dict) else data.get("label")
        return predict
    if modality == "tabular":  # CPU — off-lease
        def predict(row):
            r = httpx.post(f"{TABULAR_URL}/predict", json={"features": row}, timeout=60)
            r.raise_for_status()
            return r.json()
        return predict
    raise ValueError(f"no batch serving path for modality {modality!r}")


# --- 025 US1: assert the requested version is resident BEFORE scoring (FR-348/350, closes SC-068) ---

class BatchVersionMismatch(RuntimeError):
    """The serving tenant holds a different version than the batch requested — refuse rather than
    silently score the wrong model (FR-348/SC-068). Also raised when a controlled reload to the
    requested target is refused (e.g. a non-preemptable job holds the GPU — FR-350: never preempt)."""


# modality → the resident-serving engine base URL. GPU-lease tenants only; a tabular batch is
# CPU/off-lease and its model is selected per request, so it takes no lease assertion here.
_ENGINE_BASE = {
    "text-generation": SERVING_URL, "llm": SERVING_URL,
    "image-classification": BENTO_URL, "vision": BENTO_URL,
}
# Modalities whose served model is switched by the LLM reload verb (022 /control/reload). Vision is
# probe/assert-only (no per-version cold-load — 015's score-at-registration decision).
_RELOADABLE = {"text-generation", "llm"}


def _agent_root() -> str:
    """The host-agent control-plane root, derived from the engine URL — `/control/reload` lives at
    the agent root, not under `/engines/<id>`."""
    marker = "/engines/"
    return SERVING_URL[:SERVING_URL.index(marker)] if marker in SERVING_URL else SERVING_URL


def _resident_identity(base_url: str):
    """(model_name, registry_version) the serving engine currently holds, read from its `/health`
    (the agent reports the RESIDENT identity, 022 FR-260) — or None when unreachable/unreported. A 503
    (engine down) still carries the identity body, so status is not raised on."""
    import httpx
    try:
        d = httpx.get(f"{base_url}/health", timeout=10).json()
        return d.get("model_name"), d.get("registry_version")
    except Exception:
        return None


def _reload_to_serving(model, registry_version):
    """Ask the agent to make the active `@serving` LLM live now (022 `/control/reload`, `preempt=false`).
    A 409 means a job/GPU-batch holder would be displaced — never preempted (FR-350) — surfaced as a
    clean `BatchVersionMismatch`. This only makes the promoted `@serving` target resident; the caller
    re-probes, so a batch pinned to a non-`@serving` version is still refused (015 design)."""
    import httpx
    secret = os.getenv("AGENT_CONTROL_SECRET") or os.getenv("CONTROL_SECRET") or ""
    headers = {"X-Agent-Control": secret} if secret else {}
    r = httpx.post(f"{_agent_root()}/control/reload", headers=headers, timeout=120,
                   json={"preempt": False, "target": {"model_name": model, "version": registry_version}})
    if r.status_code == 409:
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = ""
        raise BatchVersionMismatch(
            f"cannot make {model}@{registry_version} resident for the batch: "
            f"{detail or 'reload refused (a job/batch holds the GPU — not preempted)'}")


def assert_version_resident(model, registry_version, *, probe_fn, reload_fn=None) -> dict:
    """Assert the serving tenant holds the requested `registry_version` BEFORE scoring (once per batch),
    so a batch never silently scores whatever version happens to be resident (FR-348, closes SC-068).
    Ordering only — the transport lives in `probe_fn`/`reload_fn`, so this unit-tests with no GPU and no
    live serving.

      - `registry_version is None` → the batch didn't pin a version (it means "the current @serving
        model"), so scoring the resident model IS correct; returns skipped.
      - resident version == requested → asserted, no reload.
      - resident version differs (or is unreported) and a `reload_fn` is given → make the @serving
        target live, then re-probe. `reload_fn` MUST raise (never preempt) if a job holds the GPU.
      - a CONFIRMED different resident version with no path to the requested one → BatchVersionMismatch
        (refuse; never score the wrong model). An UNREPORTED version (legacy agent) is tolerated with a
        note, matching the online `/infer` path's legacy tolerance (FR-260)."""
    if registry_version is None:
        return {"asserted": False, "reason": "no pinned version — scores the current @serving model"}
    want = str(registry_version)

    def _resident():
        ident = probe_fn()
        return (str(ident[1]) if ident and ident[1] is not None else None), ident

    have, ident = _resident()
    if have == want:
        return {"asserted": True, "resident": ident, "reloaded": False}
    if reload_fn is not None:
        reload_fn(model, registry_version)  # raises cleanly on a job/batch holder (no preemption)
        have, ident = _resident()
        if have == want:
            return {"asserted": True, "resident": ident, "reloaded": True}
    if have is not None:  # a CONFIRMED wrong version — never score it
        raise BatchVersionMismatch(
            f"batch requested version {want} of {model!r} but the serving tenant holds {have} — "
            f"refusing rather than scoring the wrong model; promote/serve {model}@{want} first "
            f"(SC-068/FR-348)")
    # have is None: the agent didn't report a version (legacy) — cannot assert; tolerate like /infer.
    return {"asserted": False, "reason": "resident version unreported — legacy tolerance (FR-260)"}


def _default_assert_fn(modality: str):
    """The live version-assertion for a GPU batch: probe the serving engine's resident identity and,
    for the reloadable LLM path, make the `@serving` target live before scoring. GPU-lease modalities
    only — returns None for tabular/unknown (off-lease, no assertion). Validated on hardware (SC-175)."""
    base = _ENGINE_BASE.get(modality)
    if base is None:
        return None
    reload_fn = _reload_to_serving if modality in _RELOADABLE else None

    def _assert(model, registry_version):
        return assert_version_resident(model, registry_version,
                                       probe_fn=lambda: _resident_identity(base), reload_fn=reload_fn)
    return _assert


@flow(name="batch-infer")
def batch_infer_flow(dataset_name: str, dataset_version: str, model: str, modality: str = "llm",
                     registry_version=None, abort_threshold: float = 0.5,
                     predict_fn=None, assert_fn=None) -> dict:
    """Score `dataset_name@dataset_version` against `model` and write a content-addressed result to Garage.
    `predict_fn`/`assert_fn` are injectable for tests; by default the per-modality live serving predictor
    and version-assertion are used. The version-assertion runs ONCE per batch, before scoring, under the
    serving lease and without preempting a running job (FR-348/350)."""
    batch = _load_batch()
    predict = predict_fn or _predict_fn(modality)
    assert_version = assert_fn if assert_fn is not None else _default_assert_fn(modality)
    if assert_version is not None:
        assert_version(model, registry_version)  # refuses (raises) rather than score the wrong version
    return batch.score_dataset(dataset_name, dataset_version, model, predict_fn=predict,
                               abort_threshold=abort_threshold, registry_version=registry_version)


if __name__ == "__main__":
    import json
    a = sys.argv
    print(json.dumps(batch_infer_flow(a[1], a[2], a[3],
                                      modality=a[4] if len(a) > 4 else "llm"), indent=2))
