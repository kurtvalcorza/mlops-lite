"""BentoML vision service (T022, US1) — the non-LLM half of FR-002.

Serves a small image classifier (MobileNetV2) packaged **from the MinIO `models` bucket** (seeded
+ registered by scripts/seed_vision_model.py — so it depends on the US2 registry). Runs on **CPU**
on purpose: it touches no VRAM, so it never contends with the single-GPU LLM serving/training
(Principle II stays about the GPU). Scale-to-zero is approximated by lazy-loading the model on the
first request and releasing it after an idle timeout — nothing resident until used.

Serve natively in WSL:  bash serving/bento/run.sh   (gateway proxies to it, like the LLM serving)
"""
import io
import os
import threading
import time

import bentoml
import boto3
import torch
from botocore.client import Config
from PIL import Image
from torchvision import transforms
from torchvision.models import mobilenet_v2

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("VISION_MODEL", "vision-mobilenet")
KEY = os.getenv("VISION_MODEL_KEY", f"{NAME}/v1/model.pt")
IDLE_TIMEOUT = float(os.getenv("VISION_IDLE_TIMEOUT", "300"))

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
        self._last_used = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._idle_watcher, daemon=True).start()

    def _ensure_loaded(self):
        """Lazy-load weights from the models bucket on first use (scale-from-zero)."""
        with self._lock:
            if self._model is None:
                blob = _s3().get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
                ckpt = torch.load(io.BytesIO(blob), map_location="cpu", weights_only=False)
                model = mobilenet_v2()
                model.load_state_dict(ckpt["state_dict"])
                model.eval()
                self._model, self._cats = model, ckpt["categories"]
            self._last_used = time.time()

    def _idle_watcher(self):
        while True:
            time.sleep(10)
            with self._lock:
                if self._model is not None and (time.time() - self._last_used) > IDLE_TIMEOUT:
                    self._model = self._cats = None  # release (scale-to-zero)

    @bentoml.api
    def classify(self, image: Image.Image) -> dict:
        """Top-5 ImageNet classification for an uploaded image."""
        self._ensure_loaded()
        x = _preprocess(image.convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            probs = self._model(x)[0].softmax(0)
        top = probs.topk(5)
        preds = [{"label": self._cats[int(i)], "score": round(float(p), 4)}
                 for p, i in zip(top.values, top.indices)]
        return {"model": NAME, "device": "cpu", "predictions": preds}

    @bentoml.api
    def info(self) -> dict:
        return {"ok": True, "loaded": self._model is not None, "model": NAME, "task": "image-classification"}
