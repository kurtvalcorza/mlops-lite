"""BentoML embeddings service (009 US2, T161 — FR-078).

Serves a sentence-transformer embedding model packaged with MLflow's `sentence_transformers` flavor
(seeded + registered by scripts/seed_embedding_model.py). Exposes
`embed(texts: list[str]) -> list[list[float]]` with `@bentoml.api(batchable=True)` so BentoML batches
texts across concurrent requests into one forward pass (CPU throughput).

**CPU-only, off the GPU lease, ALWAYS available** (grilled 2026-06-28). Unlike the vision service
(008 US2, a GPU lease tenant), embeddings never imports gpu_lease and never touches VRAM — an `embed`
call succeeds even while a GPU tenant (LLM/vision/ASR/training) holds the lease. That removes the RAG
embed→LLM swap thrash: embed (CPU) and the LLM (GPU) never contend. Lazy-load + idle-release mirror
the scale-to-zero shape of the other services (no resident cost until used), but here it's only RAM.

Serve natively in WSL:  bash serving/bento/embed_run.sh   (gateway proxies POST /embed to it)
"""
import os
import threading
import time

import bentoml

NAME = os.getenv("EMBED_MODEL", "embed-minilm")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5500")
SERVING_ALIAS = os.getenv("EMBED_ALIAS", "serving")
IDLE_TIMEOUT = float(os.getenv("EMBED_IDLE_TIMEOUT", "600"))
# Fallback model id if the registry has no @serving embedding version yet (offline-friendly; the seed
# uses the same id so the served vectors match the registered model).
FALLBACK_MODEL_ID = os.getenv("EMBED_MODEL_ID", "sentence-transformers/all-MiniLM-L6-v2")


def _load_model():
    """Load the embedding model on CPU. Prefer the registry @serving version via the MLflow
    sentence_transformers flavor (FR-078); fall back to loading the model id directly so the service
    still starts if the registry is empty/unreachable (always-available posture)."""
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


@bentoml.service(name="embedding-service", traffic={"timeout": 60})
class EmbeddingService:
    def __init__(self) -> None:
        self._model = None
        self._source = None
        self._last_used = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._idle_watcher, daemon=True).start()

    def _ensure_loaded(self):
        """Caller holds self._lock. Lazy-load on first use (scale-from-zero); CPU-only, no lease."""
        if self._model is None:
            self._model, self._source = _load_model()
        self._last_used = time.time()

    def _idle_watcher(self):
        while True:
            time.sleep(30)
            with self._lock:
                if self._model is not None and (time.time() - self._last_used) > IDLE_TIMEOUT:
                    self._model = self._source = None  # drop RAM; nothing to release (off-lease)

    @bentoml.api(batchable=True)
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts → a list of equal-dimension float vectors (one per text).

        `batchable=True` lets BentoML concatenate texts from concurrent requests into one
        `encode()` forward pass and split the result back per request (CPU batching throughput).
        """
        with self._lock:
            self._ensure_loaded()
            vecs = self._model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=False)
            self._last_used = time.time()
        return [[float(x) for x in row] for row in vecs]

    @bentoml.api
    def info(self) -> dict:
        return {"ok": True, "loaded": self._model is not None, "model": NAME,
                "source": self._source, "device": "cpu", "task": "embedding", "lease": "off"}
