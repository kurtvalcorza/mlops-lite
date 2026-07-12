"""Platform-wide GPU/daemon metrics, proxied through the gateway (T043).

The serving supervisor and training daemon run natively in WSL, outside the container engine —
Prometheus (in a container) can't reach their dynamic IPs cleanly. Rather than wire a fragile
cross-distro scrape, the gateway (which already knows the daemons' injected URLs) polls their
health on each /metrics scrape and re-exports the GPU/state signals as its own gauges. Prometheus
then gets everything from the one gateway target it already scrapes.
"""

import httpx
from prometheus_client import Gauge

from . import settings

AGENT_URL = settings.AGENT_URL

GPU_FREE = Gauge("mlops_gpu_free_mib", "Free GPU memory (MiB) reported by the host agent")
SERVING_RESIDENT = Gauge("mlops_serving_resident", "1 if a model is resident in the serving GPU")
TRAINER_BUSY = Gauge("mlops_trainer_busy", "1 if a training run is currently active")
SERVING_UP = Gauge("mlops_serving_up", "1 if the serving surface (host agent) is reachable")
TRAINER_UP = Gauge("mlops_trainer_up", "1 if the jobs surface (host agent) is reachable")


def refresh() -> None:
    """Best-effort poll of the host agent; called when /metrics is scraped. T363: the serving + jobs
    signals both come from the ONE agent `/health` now (serving supervisor + training daemon folded
    in), so a single read backs the (retained) Prometheus gauge names."""
    try:
        with httpx.Client(headers=settings.agent_headers(), timeout=2) as client:
            h = client.get(f"{AGENT_URL}/health").json()
    except Exception:
        SERVING_UP.set(0)
        TRAINER_UP.set(0)
        return
    SERVING_UP.set(1)
    TRAINER_UP.set(1)
    # Resident ⇔ the LLM child is alive — `ready` OR `loading` (@claude PR#37: the old supervisor's
    # `resident` flag was child-alive, true during cold-start too; keying on `ready` alone read 0 in
    # the spawned-but-not-ready window where the old gauge read 1).
    SERVING_RESIDENT.set(1 if h.get("engines", {}).get("llm") in ("ready", "loading") else 0)
    TRAINER_BUSY.set(1 if h.get("busy") else 0)
    if h.get("gpu_free_mib") is not None:
        GPU_FREE.set(h["gpu_free_mib"])
