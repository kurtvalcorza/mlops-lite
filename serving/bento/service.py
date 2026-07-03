"""BentoML vision service (T022, US1; 018 T360 — GPU-lease code stripped, agent-managed).

Serves a small image classifier (MobileNetV2) packaged **from the MinIO `models` bucket** (seeded
+ registered by scripts/seed_vision_model.py — so it depends on the US2 registry).

**018 T360**: this service is now a **child of the GPU host agent** (hostagent/adapters/vision.py).
Admission (the single race-free GPU lease, "one model in VRAM") is the AGENT's job — the agent
acquires the `vision` lease before spawning this child and frees the VRAM by killing it, so the
service's own 008 in-service lease/idle-release/unload-now/busy-marker code is removed. What remains
is the pure model: lazy-load onto DEVICE on first request, classify, and report `info`. It still
reuses the frozen `torchvision==0.26.0+cu128` in the native env (no new dependency, 007 FR-060); off
a GPU it loads on CPU. The agent stays torch-free (R10) — the torch model lives only here.

Spawned by the agent via serving/bento/run.sh on a dynamic loopback port (the gateway proxies to the
agent's /engines/vision, like the LLM/ASR engines).
"""
import io
import os
import sys
import threading

import bentoml
import boto3
import torch
from botocore.client import Config
from PIL import Image
from torchvision import transforms
from torchvision.models import mobilenet_v2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # serving/ on path

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("VISION_MODEL", "vision-mobilenet")
KEY = os.getenv("VISION_MODEL_KEY", f"{NAME}/v1/model.pt")
# One GPU tenant in VRAM (Principle II): use the GPU when present, else CPU. The agent's admission
# guarantees no co-residency; this service just loads onto whichever device is available.
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

_preprocess = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])


def _s3():
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],  # no hardcoded default (FR-017)
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"))


@bentoml.service(name="vision-classifier", traffic={"timeout": 60})
class VisionClassifier:
    def __init__(self) -> None:
        self._model = None
        self._cats = None
        self._lock = threading.Lock()  # serialize the lazy load so concurrent classifies don't race

    def _ensure_loaded_locked(self):
        """Lazy-load weights from the models bucket on first use (scale-from-zero). Caller holds
        self._lock so two concurrent classifies can't double-load the model onto VRAM.

        018 T360: no GPU-lease calls here — the agent holds the `vision` lease around this whole
        child's lifetime, so loading onto CUDA is safe (never co-resident with the LLM/ASR/training)
        without this service participating in the lease itself."""
        if self._model is not None:
            return
        blob = _s3().get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
        ckpt = torch.load(io.BytesIO(blob), map_location="cpu", weights_only=False)
        # Size the head from the checkpoint's `categories` so a 010 head-swapped fine-tune (K≠1000
        # classes) loads, not just the 1000-class ImageNet seed.
        cats = ckpt["categories"]
        model = mobilenet_v2(num_classes=len(cats))
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        if DEVICE == "cuda":
            model = model.to("cuda")
        self._model, self._cats = model, cats

    @bentoml.api
    def classify(self, image: Image.Image) -> dict:
        """Top-5 ImageNet classification for an uploaded image. Holds self._lock across the lazy
        load + every GPU op so a concurrent request can't load the model twice or read a half-built
        one. CPU-side preprocessing runs outside the lock; GPU results are reduced to plain Python
        inside it, so the response is built lock-free.

        018 T360: GPU-lease contention is refused by the AGENT (409) before the request ever reaches
        this child, so the old in-service `{"busy": true}` marker is gone."""
        x = _preprocess(image.convert("RGB")).unsqueeze(0)  # CPU preprocessing — outside the lock
        with self._lock:
            self._ensure_loaded_locked()
            if DEVICE == "cuda":
                x = x.to("cuda")
            with torch.no_grad():
                probs = self._model(x)[0].softmax(0)
                top = probs.topk(5)
            scores = [round(float(p), 4) for p in top.values]
            labels = [self._cats[int(i)] for i in top.indices]
        preds = [{"label": lbl, "score": sc} for lbl, sc in zip(labels, scores)]
        return {"model": NAME, "device": DEVICE, "predictions": preds}

    @bentoml.api
    def info(self) -> dict:
        return {"ok": True, "loaded": self._model is not None, "model": NAME,
                "device": DEVICE, "task": "image-classification"}
