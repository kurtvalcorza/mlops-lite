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

**Known live gap (inherited SC-068, deferred to the on-hardware step).** `_predict_fn` scores whichever
version the serving tenant currently holds — it does **not** load/assert `model`/`registry_version`
before scoring. Those identifiers are recorded in the result manifest for provenance, but on real
hardware a batch launched while serving holds a *different* version would silently score that resident
version. This is the same gap 011's `/compare` and 012's `eval_fn` documented; on-demand per-version
loading is the hardware-validated fix. Until then, promote/serve the intended version first, or inject a
`predict_fn` (tests do). The mismatch is not yet asserted because the gateway can't introspect the native
supervisor's resident version off the request path.

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
            # The tabular child takes a BATCH body `{"rows": [...]}` and returns
            # `{"predictions": [{"prediction", "score"}, ...]}` (serving/children/tabular_service.py) —
            # NOT a per-row `{"features": row}`, which 422s. Send one row, return its single prediction.
            r = httpx.post(f"{TABULAR_URL}/predict", json={"rows": [row]}, timeout=60)
            r.raise_for_status()
            preds = r.json().get("predictions") or []
            return preds[0] if preds else None
        return predict
    raise ValueError(f"no batch serving path for modality {modality!r}")


@flow(name="batch-infer")
def batch_infer_flow(dataset_name: str, dataset_version: str, model: str, modality: str = "llm",
                     registry_version=None, abort_threshold: float = 0.5,
                     predict_fn=None) -> dict:
    """Score `dataset_name@dataset_version` against `model` and write a content-addressed result to Garage.
    `predict_fn` is injectable for tests; by default the per-modality live serving predictor is used."""
    batch = _load_batch()
    predict = predict_fn or _predict_fn(modality)
    return batch.score_dataset(dataset_name, dataset_version, model, predict_fn=predict,
                               abort_threshold=abort_threshold, registry_version=registry_version)


if __name__ == "__main__":
    import json
    a = sys.argv
    print(json.dumps(batch_infer_flow(a[1], a[2], a[3],
                                      modality=a[4] if len(a) > 4 else "llm"), indent=2))
