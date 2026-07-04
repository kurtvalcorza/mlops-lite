"""Slim embeddings child (020 US2, T408) — FastAPI single-route replacement for the BentoML
embeddings service (FR-203; contracts/children-api.md).

The sentence-transformers code moves verbatim from `serving/bento/embed_service.py`; only the
route scaffolding changes (BentoML → one FastAPI `POST /embed` + `GET /readyz`). Same JSON
request (`{"texts": [...]}`), same bare `list[list[float]]` response, same dynamic-port launch
contract (embed_run.sh honors BENTO_HOST/BENTO_PORT). BentoML's cross-request `batchable=True`
micro-batching is the one behavior not carried over — it was a throughput optimization, not
part of the route contract; each request still encodes its own batch of texts in one forward
pass.

CPU-only, off GPU admission, ALWAYS available: embeddings never touch VRAM or the agent's GPU
slot (the run script hides the GPU outright via CUDA_VISIBLE_DEVICES=""). Lazy-load +
idle-release keep the scale-to-zero shape — only RAM here.
"""
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

NAME = os.getenv("EMBED_MODEL", "embed-minilm")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
SERVING_ALIAS = os.getenv("EMBED_ALIAS", "serving")
IDLE_TIMEOUT = float(os.getenv("EMBED_IDLE_TIMEOUT", "600"))
# Fallback model id if the registry has no @serving embedding version yet (offline-friendly; the
# seed uses the same id so the served vectors match the registered model).
FALLBACK_MODEL_ID = os.getenv("EMBED_MODEL_ID", "sentence-transformers/all-MiniLM-L6-v2")


def _load_model():
    """Load the embedding model on CPU. Prefer the registry @serving version via the MLflow
    sentence_transformers flavor (FR-078); fall back to loading the model id directly so the
    service still starts if the registry is empty/unreachable (always-available posture)."""
    import mlflow.sentence_transformers as st_flavor  # heavy import — deferred to first use

    try:
        model = st_flavor.load_model(f"models:/{NAME}@{SERVING_ALIAS}")
        source = f"models:/{NAME}@{SERVING_ALIAS}"
    except Exception:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(FALLBACK_MODEL_ID, device="cpu")
        source = FALLBACK_MODEL_ID
    # Force CPU regardless of how it was loaded — embeddings are deliberately off the GPU lease.
    try:
        model = model.to("cpu")
    except Exception:
        pass
    return model, source


@asynccontextmanager
async def _lifespan(app):
    # Start the idle-release watcher at APP startup, not module import (review, PR#55): merely
    # importing this module from a test/tool must not spin a forever-thread that polls S3/MLflow.
    threading.Thread(target=_idle_watcher, daemon=True).start()
    yield


app = FastAPI(lifespan=_lifespan)

_model = None
_source = None
_last_used = 0.0
_lock = threading.Lock()


def _ensure_loaded():
    """Caller holds _lock. Lazy-load on first use (scale-from-zero); CPU-only, no lease."""
    global _model, _source, _last_used
    if _model is None:
        _model, _source = _load_model()
    _last_used = time.time()


def _idle_watcher():
    global _model, _source
    while True:
        time.sleep(30)
        with _lock:
            if _model is not None and (time.time() - _last_used) > IDLE_TIMEOUT:
                _model = _source = None  # drop RAM; nothing to release (off-lease)



class EmbedRequest(BaseModel):
    texts: list[str]


@app.get("/readyz")
def readyz() -> dict:
    return {"ok": True}


@app.post("/embed")
def embed(req: EmbedRequest) -> list[list[float]]:
    """Embed a batch of texts → a list of equal-dimension float vectors (one per text) — the
    same bare-array response the BentoML child returned."""
    global _last_used
    with _lock:
        _ensure_loaded()
        vecs = _model.encode(list(req.texts), convert_to_numpy=True, normalize_embeddings=False)
        _last_used = time.time()
    return [[float(x) for x in row] for row in vecs]
