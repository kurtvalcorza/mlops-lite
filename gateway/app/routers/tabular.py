"""Tabular router (009 US4, T172 — FR-080/085): proxy tabular prediction to the tabular service.

The tabular service runs natively in WSL on CPU, **off the GPU lease** — so, like /embed and unlike
/infer or /transcribe, there is no lease/busy handling: a /predict call succeeds even while a GPU
tenant holds the lease (always-available, CPU-only). Thin proxy, mirroring vision.py/embed.py;
up_all.ps1 injects the service IP via TABULAR_URL.

**025 US2 (T605, FR-353) — prediction logging + identity.** Tabular is now a full quality participant,
so each served row is logged like a vision/LLM prediction: a **per-row `prediction_id`** returned to the
caller (so a delayed ground-truth label can be attached later) and a version-scoped index row so
`quality.window()` has something to join. Two tabular-specific rules:

  - `/predict` is a BATCH endpoint (N rows → N predictions), so N prediction ids are minted, one per
    row, and returned positionally alongside the predictions.
  - the logged prediction value is the **numeric `score`** (the probability), never the thresholded
    0/1 `prediction`: `quality.score_window` feeds stored values straight to `evaluation.auc`, which
    RANKS them — the binary class discards exactly the ordering AUC measures.

Logging stays fire-and-forget + fail-open off the response path (FR-119), so a slow store never adds
latency to a served prediction and never breaks it.
"""

import os
import uuid

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter
from pydantic import BaseModel

from .. import background, quality, registry
from ..settings import TABULAR_URL, agent_headers

router = APIRouter()

TABULAR_REQUESTS = Counter("gateway_predict_total", "Tabular predict requests", ["status"])

#: Disambiguator when several `task=tabular` models hold a promoted alias (mirrors VISION_SERVING_MODEL).
TABULAR_SERVING_MODEL = os.getenv("TABULAR_SERVING_MODEL") or os.getenv("TABULAR_MODEL")

TASK = "tabular"


def _resolve_tabular_version(served=None) -> tuple:
    """Best-effort (model_name, version) to attribute a served row to, for prediction logging —
    never raises (None on any failure). Blocking → call off the event loop.

    Prefers the version the CHILD reports it actually scored with (`model_version`) over a registry
    read, the same agent-reported-identity rule 022 FR-260 established for the LLM path. The two can
    legitimately disagree: the alias moves the instant a promote lands, but the child only picks the
    new booster up on its next version check — so attributing to the registry alone would log rows
    produced by the OLD booster under the NEW version and quietly poison that version's quality
    window with another model's outputs. The registry stays the fallback for a child too old to
    report its version.
    """
    if served and served.get("version") is not None:
        return (served.get("model") or TABULAR_SERVING_MODEL, str(served["version"]))
    try:
        target = registry.resolve_serving_target(TASK, TABULAR_SERVING_MODEL)
        return (target["name"], target["version"]) if target else (None, None)
    except Exception:  # noqa: BLE001 — attribution is best-effort; logging must never break serving
        return (None, None)


def _row_scores(data) -> list:
    """The per-row NUMERIC score from the child's response, in row order.

    The child returns `{"predictions": [{"prediction": 0|1, "score": float}, ...]}`. AUC ranks the
    numeric score, so that is what gets logged — falling back to `prediction` only when a response
    carries no score (an older child), which is honest about what was available rather than silently
    logging a class as if it were a probability. Guards on shape first: a non-dict/odd response must
    never raise out of the fail-open logging path."""
    if not isinstance(data, dict):
        return []
    out = []
    for p in data.get("predictions") or []:
        if isinstance(p, dict):
            out.append(p.get("score", p.get("prediction")))
        else:
            out.append(p)
    return out


class PredictRequest(BaseModel):
    rows: list[dict]


@router.post("/predict")
async def predict(req: PredictRequest):
    """One prediction per input row from the LightGBM joblib artifact (CPU, off-lease)."""
    if not req.rows:
        raise HTTPException(status_code=400, detail="rows must be a non-empty list of objects")
    async with httpx.AsyncClient(headers=agent_headers(), timeout=60) as client:
        try:
            r = await client.post(f"{TABULAR_URL}/predict", json={"rows": req.rows})
        except httpx.HTTPError as e:
            TABULAR_REQUESTS.labels(status="unavailable").inc()
            raise HTTPException(status_code=503, detail=f"tabular service unreachable at {TABULAR_URL}: {e}")
    if r.status_code != 200:
        TABULAR_REQUESTS.labels(status="error").inc()
        raise HTTPException(status_code=502, detail=f"tabular service error {r.status_code}: {r.text[:200]}")
    TABULAR_REQUESTS.labels(status="ok").inc()
    data = r.json()

    # 025 US2 (T605/FR-353): log each served row fully OFF the response path. Ids are generated
    # SYNCHRONOUSLY (returned to the caller regardless of whether the store write lands), while the
    # version resolve + the store writes run fire-and-forget — so a slow/unreachable registry adds no
    # latency to the served prediction (the vision/LLM discipline, FR-119).
    scores = _row_scores(data)
    pids = [uuid.uuid4().hex for _ in range(len(scores))]
    # Snapshot the child's reported identity from THIS response, before awaiting anything: it names
    # the booster that produced these very rows, so a promote landing mid-flight cannot re-attribute
    # them (the child reloads on a version change — serving/children/tabular_service.py).
    served = {"model": data.get("model"), "version": data.get("model_version")} \
        if isinstance(data, dict) else None

    async def _log():
        name, version = await run_in_threadpool(_resolve_tabular_version, served)
        for pid, score, row in zip(pids, scores, req.rows):
            # The numeric score, never the thresholded class — quality.score_window ranks these.
            quality.log_prediction(name, version, TASK, None, score, prediction_id=pid)
            # Capture the recoverable feature row so a challenger can be shadow-replayed over real
            # traffic (016/FR-146) — bounded, opt-in, fail-open like every capture.
            quality.capture_input(pid, TASK, row)

    if pids:
        # 018/FR-164: retained (not detached) — a GC'd task would silently drop the prediction log.
        background.spawn(_log(), kind="tabular-log")
    # Return the ids positionally so a caller can attach a delayed ground-truth label per row
    # (the label endpoint takes a caller-supplied prediction_id, and there is no prediction-list API).
    if isinstance(data, dict) and pids:
        preds = data.get("predictions")
        if isinstance(preds, list) and len(preds) == len(pids):
            data = {**data, "predictions": [
                {**p, "prediction_id": pid} if isinstance(p, dict) else p
                for p, pid in zip(preds, pids)]}
        data = {**data, "prediction_ids": pids}
    return data


@router.get("/predict/health")
async def predict_health():
    async with httpx.AsyncClient(headers=agent_headers(), timeout=5) as client:
        try:
            r = await client.get(f"{TABULAR_URL}/readyz")
            return {"backend": "bentoml tabular (native WSL, CPU, off-lease)",
                    "reachable": r.status_code == 200}
        except httpx.HTTPError:
            return {"backend": "bentoml tabular (native WSL, CPU, off-lease)", "reachable": False}
