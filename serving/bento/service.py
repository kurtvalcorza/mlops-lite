"""BentoML vision service (T022, US1; 008 US2 — vision-on-GPU under the lease).

Serves a small image classifier (MobileNetV2) packaged **from the MinIO `models` bucket** (seeded
+ registered by scripts/seed_vision_model.py — so it depends on the US2 registry).

008 US2: vision is now a **GPU lease tenant** (FR-066/067). When CUDA is present it loads the model
onto the **GPU**, holds the single race-free lease (serving/gpu_lease.py) while resident — so it is
**never co-resident** with the LLM or a training run (strict one-model-in-VRAM now includes vision) —
and releases the lease on idle, mirroring the LLM supervisor. It reuses the frozen
`torchvision==0.26.0+cu128` already in the native env (**no new dependency**, 007 FR-060). Off a GPU
(CUDA absent) it falls back to CPU and holds no lease — CPU-only inference is lease-exempt (Principle
II is about VRAM). Scale-to-zero is the lazy load + idle release: nothing resident until used.

Serve natively in WSL:  bash serving/bento/run.sh   (gateway proxies to it, like the LLM serving)
"""
import io
import os
import sys
import threading
import time

import bentoml
import boto3
import torch
from botocore.client import Config
from PIL import Image
from torchvision import transforms
from torchvision.models import mobilenet_v2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # serving/ on path
import gpu_lease  # noqa: E402  (shared, stdlib-only GPU lease — 008 US2)

S3_ENDPOINT = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
BUCKET = os.getenv("MODELS_BUCKET", "models")
NAME = os.getenv("VISION_MODEL", "vision-mobilenet")
KEY = os.getenv("VISION_MODEL_KEY", f"{NAME}/v1/model.pt")
IDLE_TIMEOUT = float(os.getenv("VISION_IDLE_TIMEOUT", "300"))
LEASE_TENANT = "vision"                                  # this daemon's GPU lease identity (008 US2)
VRAM_GB = float(os.getenv("VRAM_GB", "12"))              # static-budget fallback if GPU unreadable
VISION_EST_GB = float(os.getenv("VISION_EST_GB", "1.0"))  # MobileNet + CUDA context (tiny, but non-zero)
# One GPU tenant in VRAM (Principle II): use the GPU when present, else CPU (CPU is lease-exempt).
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
if DEVICE == "cuda":
    # 018/FR-166: a lease participant must observe the same coordination state as its peers —
    # fail loud at startup on divergence rather than co-reside on the GPU later.
    gpu_lease.verify_shared_state_dir("bento-vision")

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

    def _ensure_loaded_locked(self):
        """Lazy-load weights from the models bucket on first use (scale-from-zero).

        **Caller must hold self._lock** (Claude review F2). A classify holds the lease's guard
        across load + inference, and the idle watcher takes the same lock to release — so the lease
        can never be released mid-inference. That closes both a NoneType-call crash and a Principle
        II co-residency window (another tenant loading onto the GPU while a vision inference is still
        in flight on CUDA).

        008 US2: on a GPU, acquire the single lease before touching VRAM (FR-066/067) so vision is
        never co-resident with the LLM/training; load the model onto CUDA; release the lease if the
        load fails (no deadlock). Off a GPU, load on CPU and hold no lease (CPU is lease-exempt).
        """
        if DEVICE == "cuda":
            # Re-affirm the lease on EVERY use (idempotent same-PID if we already hold it), not just
            # on a cold load — so vision never runs on the GPU without the lease even if the lease
            # were lost while the model stayed resident. If we don't hold it, drop any stale resident
            # model so it stops occupying VRAM, then propagate (classify → structured busy response).
            try:
                gpu_lease.acquire(LEASE_TENANT, est_gb=VISION_EST_GB, vram_budget_gb=VRAM_GB)
            except (gpu_lease.LeaseHeld, gpu_lease.VramExceeded):
                if self._model is not None:
                    self._model = self._cats = None
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                raise
        if self._model is None:
            try:
                blob = _s3().get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
                ckpt = torch.load(io.BytesIO(blob), map_location="cpu", weights_only=False)
                # Size the classifier head from the checkpoint's `categories` so a 010 head-swapped
                # fine-tune (K≠1000 classes) loads, not just the 1000-class ImageNet seed. Backward
                # compatible: the seed carries 1000 categories → mobilenet_v2(num_classes=1000), which
                # is the default arch. The single server-side touch 010 needs (the flow fine-tunes into
                # this exact {state_dict, categories} shape).
                cats = ckpt["categories"]
                model = mobilenet_v2(num_classes=len(cats))
                model.load_state_dict(ckpt["state_dict"])
                model.eval()
                if DEVICE == "cuda":
                    model = model.to("cuda")
                self._model, self._cats = model, ckpt["categories"]
            except BaseException:
                if DEVICE == "cuda":
                    gpu_lease.release(LEASE_TENANT)  # load failed → free the slot
                raise
        self._last_used = time.time()

    def _release(self):
        """Drop the resident model (scale-to-zero) and free the GPU lease. Caller holds self._lock."""
        self._model = self._cats = None
        if DEVICE == "cuda":
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            gpu_lease.release(LEASE_TENANT)

    def _idle_watcher(self):
        while True:
            time.sleep(10)
            with self._lock:
                if self._model is not None and (time.time() - self._last_used) > IDLE_TIMEOUT:
                    self._release()  # release VRAM + the lease for the next tenant (FR-071)

    @bentoml.api
    def classify(self, image: Image.Image) -> dict:
        """Top-5 ImageNet classification for an uploaded image (on the GPU under the lease).

        Holds self._lock across load + every GPU op so the idle watcher cannot release the lease
        while an inference is in flight (Claude review F2 — no co-residency, no NoneType crash).
        CPU-side preprocessing runs outside the lock; the GPU results are reduced to plain Python
        numbers inside the lock, so the response is built lock-free.

        On GPU-lease contention (another tenant holds the GPU) this returns a structured busy
        marker — {"busy": true, "detail": "..."} at HTTP 200 — instead of raising a 5xx, so the
        gateway can map it to a clean 409 GPU-busy with the hint (Codex #6; BentoML masks 5xx
        bodies, so the message wouldn't survive a raised ServiceUnavailable).
        """
        x = _preprocess(image.convert("RGB")).unsqueeze(0)  # CPU preprocessing — outside the lock
        try:
            with self._lock:
                self._ensure_loaded_locked()  # acquires the lease; raises LeaseHeld/VramExceeded if held
                if DEVICE == "cuda":
                    x = x.to("cuda")
                with torch.no_grad():
                    probs = self._model(x)[0].softmax(0)
                    top = probs.topk(5)
                scores = [round(float(p), 4) for p in top.values]
                labels = [self._cats[int(i)] for i in top.indices]
                self._last_used = time.time()
        except gpu_lease.LeaseHeld as e:
            holder = (e.holder or {}).get("tenant", "another GPU tenant")
            return {"busy": True, "device": DEVICE, "predictions": [],
                    "detail": f"GPU busy: {holder} holds the GPU (one model in VRAM, Principle II); "
                              f"free it (idle-release or stop serving) to classify"}
        except gpu_lease.VramExceeded as e:
            return {"busy": True, "device": DEVICE, "predictions": [], "detail": str(e)}
        preds = [{"label": lbl, "score": sc} for lbl, sc in zip(labels, scores)]
        return {"model": NAME, "device": DEVICE, "predictions": preds}

    @bentoml.api(route="/unload-now")
    def unload_now(self, drain_timeout_s: float = 10, ctx: bentoml.Context = None) -> dict:
        """017 (FR-156): the gateway's swap control call — drain an in-flight classify within
        `drain_timeout_s`, then drop the resident model + release the GPU lease so the target serving
        model can load. `self._lock` is held for the whole duration of a classify (load + GPU op), so
        acquiring it === drained and blocks new work; on timeout we hard-release (best-effort — vision
        inference is sub-second, so a clean drain is the norm). Idempotent: not loaded → `idle`.

        Off a GPU (CPU, lease-exempt) there is nothing to swap — drop the model so a later request reloads
        but report `idle` since no lease was involved.

        Acquire the lock **first** rather than fast-pathing on `self._model is None`: during a cold load,
        `_ensure_loaded_locked` holds the lease + the lock but hasn't assigned `self._model` yet, so an
        early `is None` check would report `idle` while vision genuinely holds the GPU — the gateway would
        then wait out SWAP_FREE_WAIT_S and fail while the classify completes and stays resident. Holding
        the lock === no classify/cold-load is in flight, so a `None` model under the lock is truly idle.

        Opt-in defense-in-depth (like the native supervisors): when SWAP_CONTROL_SECRET is set, require the
        X-Swap-Control header the gateway forwards; unset → 008 behavior. BentoML masks raised 5xx bodies,
        so refuse via a structured status the gateway maps to a clean swap-failure (as the busy path does)."""
        secret = os.getenv("SWAP_CONTROL_SECRET", "")
        if secret:
            try:
                got = ctx.request.headers.get("x-swap-control", "") if ctx is not None else ""
            except Exception:
                got = ""
            if got != secret:
                return {"status": "unauthorized", "device": DEVICE,
                        "detail": "unauthorized unload-now (bad or missing X-Swap-Control)"}
        acquired = self._lock.acquire(timeout=max(float(drain_timeout_s), 0.0))
        if not acquired:
            # Vision is an IN-PROCESS torch model — unlike the subprocess llama/whisper supervisors, whose
            # hard-cut kills the model proc and frees VRAM *before* releasing the lease. Dropping the model
            # + releasing the lease here, while an in-flight classify is still computing on CUDA under the
            # lock, would momentarily co-reside with the next tenant (Principle II violation) and risk a
            # NoneType mid-inference. Vision inference is sub-second, so a drain rarely times out; when it
            # does we REFUSE the swap (status != unloaded → the gateway surfaces a clean 409) rather than
            # break the one-model-in-VRAM invariant. The operator can retry the swap.
            return {"status": "busy", "drained": False, "device": DEVICE,
                    "detail": "in-flight classify/cold-load did not drain within the timeout — retry the swap"}
        try:
            if self._model is None:
                return {"status": "idle"}  # lock free ⇒ no classify/cold-load in flight ⇒ truly idle
            self._release()  # drops the model + (on GPU) releases the lease — under the lock, never racy
            return {"status": "unloaded", "drained": True, "device": DEVICE}
        finally:
            self._lock.release()

    @bentoml.api
    def info(self) -> dict:
        return {"ok": True, "loaded": self._model is not None, "model": NAME,
                "device": DEVICE, "task": "image-classification"}
