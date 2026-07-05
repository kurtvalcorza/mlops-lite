"""Slim vision child (020 US2, T407) — FastAPI single-route replacement for the BentoML
vision service (FR-203; contracts/children-api.md).

The torch/torchvision MobileNet code moves verbatim from `serving/bento/service.py`; only the
route scaffolding changes (BentoML → one FastAPI `POST /classify` + `GET /readyz`). Same
multipart request (field `image`), same `{model, device, predictions}` response values, same
dynamic-port launch contract (serving/children/run.sh honors BENTO_HOST/BENTO_PORT). The agent
owns admission — it acquires the `vision` lease before spawning this child and frees the VRAM
by killing it — so there is no lease/unload/busy code here (018 T360 posture, unchanged).

Serves a small image classifier (MobileNetV2) packaged from the S3 `models` bucket (seeded +
registered by scripts/seed_vision_model.py). Lazy-load onto DEVICE on first request; off a GPU
it loads on CPU.
"""
import io
import os
import threading

import boto3
import torch
from botocore.client import Config
from fastapi import FastAPI, File, UploadFile
from PIL import Image
from torchvision import transforms
from torchvision.models import mobilenet_v2

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:3900")
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


app = FastAPI()

_model = None
_cats = None
_lock = threading.Lock()  # serialize the lazy load so concurrent classifies don't race


def _ensure_loaded_locked():
    """Lazy-load weights from the models bucket on first use (scale-from-zero). Caller holds
    _lock so two concurrent classifies can't double-load the model onto VRAM.

    No GPU-lease calls here — the agent holds the `vision` lease around this whole child's
    lifetime, so loading onto CUDA is safe (never co-resident with the LLM/ASR/training)
    without this service participating in the lease itself."""
    global _model, _cats
    if _model is not None:
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
    _model, _cats = model, cats


@app.get("/readyz")
def readyz() -> dict:
    return {"ok": True}


@app.post("/classify")
async def classify(image: UploadFile = File(...)) -> dict:
    """Top-5 classification for an uploaded image (multipart field `image`, same as the BentoML
    child). Holds _lock across the lazy load + every GPU op so a concurrent request can't load
    the model twice or read a half-built one. CPU-side preprocessing runs outside the lock; GPU
    results are reduced to plain Python inside it, so the response is built lock-free.

    GPU-lease contention is refused by the AGENT (409) before the request ever reaches this
    child."""
    raw = await image.read()
    x = _preprocess(Image.open(io.BytesIO(raw)).convert("RGB")).unsqueeze(0)  # outside the lock
    with _lock:
        _ensure_loaded_locked()
        if DEVICE == "cuda":
            x = x.to("cuda")
        with torch.no_grad():
            probs = _model(x)[0].softmax(0)
            top = probs.topk(5)
        scores = [round(float(p), 4) for p in top.values]
        labels = [_cats[int(i)] for i in top.indices]
    preds = [{"label": lbl, "score": sc} for lbl, sc in zip(labels, scores)]
    return {"model": NAME, "device": DEVICE, "predictions": preds}
