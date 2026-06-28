"""Platform-wide GPU/daemon metrics, proxied through the gateway (T043).

The serving supervisor and training daemon run natively in WSL, outside the container engine —
Prometheus (in a container) can't reach their dynamic IPs cleanly. Rather than wire a fragile
cross-distro scrape, the gateway (which already knows the daemons' injected URLs) polls their
health on each /metrics scrape and re-exports the GPU/state signals as its own gauges. Prometheus
then gets everything from the one gateway target it already scrapes.
"""
import os

import httpx
from prometheus_client import Gauge

SERVING_URL = os.getenv("SERVING_URL", "http://host.docker.internal:8090")
TRAINER_URL = os.getenv("TRAINER_URL", "http://host.docker.internal:8091")

GPU_FREE = Gauge("mlops_gpu_free_mib", "Free GPU memory (MiB) reported by the training daemon")
SERVING_RESIDENT = Gauge("mlops_serving_resident", "1 if a model is resident in the serving GPU")
TRAINER_BUSY = Gauge("mlops_trainer_busy", "1 if a training run is currently active")
SERVING_UP = Gauge("mlops_serving_up", "1 if the serving supervisor is reachable")
TRAINER_UP = Gauge("mlops_trainer_up", "1 if the training daemon is reachable")


def refresh() -> None:
    """Best-effort poll of the native daemons; called when /metrics is scraped."""
    with httpx.Client(timeout=2) as client:
        try:
            h = client.get(f"{SERVING_URL}/health").json()
            SERVING_UP.set(1)
            SERVING_RESIDENT.set(1 if h.get("resident") else 0)
        except Exception:
            SERVING_UP.set(0)
        try:
            h = client.get(f"{TRAINER_URL}/health").json()
            TRAINER_UP.set(1)
            TRAINER_BUSY.set(1 if h.get("busy") else 0)
            if h.get("gpu_free_mib") is not None:
                GPU_FREE.set(h["gpu_free_mib"])
        except Exception:
            TRAINER_UP.set(0)
