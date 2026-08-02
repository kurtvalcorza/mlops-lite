"""Per-device GPU topology snapshot (027 T708 — contracts/runtime-api.md `GET /runtime/devices`).

Two constraints shape this module, and both are things a straightforward implementation gets wrong:

**No subprocess per request.** The snapshot rides the **existing 1-second-TTL cached reader**. Forking
`nvidia-smi` on every console poll is the exact 018 regression NVML was introduced to remove, and a
console with ten live panels would reintroduce it at ten times the rate. NVML is queried in-process;
`nvidia-smi` is a fallback taken only on a cache miss when NVML is unavailable.

**`static` means null, never zero.** When the GPU cannot be read, every per-device field except
`index` is `null` and `source` is `"static"`. A caller must not substitute zero — "0 GB free" is a
*false* reading that an operator would act on, whereas `null` plus a labelled source is an honest
"unknown". This is FR-381's fallback labelling as data rather than as a note in a docstring.

The snapshot is **side-effect free**. It may take the admission lock for a consistent read; it never
claims, extends, or releases (Principle II — the console reads admission and never mutates it).
"""
import time

_GIB = 1024 ** 3

#: Source provenance, in descending fidelity. Carried in the payload so the console labels a
#: fallback-derived value rather than inferring which path produced it.
NVML = "nvml"
SMI = "smi"
STATIC = "static"


class DeviceSnapshotter:
    """Builds the device list, caching whole snapshots for `ttl_s`.

    The cache is on the *snapshot*, not on individual readings: a device list assembled from readings
    taken at different instants would show a total and a free that never coexisted, which is the kind
    of internally-inconsistent number an operator reasonably reports as a bug.
    """

    def __init__(self, ttl_s: float = 1.0, clock=time.monotonic, nvml_fn=None, smi_fn=None,
                 engine_pids=None):
        self._ttl = ttl_s
        self._clock = clock
        self._nvml = nvml_fn or _read_nvml
        self._smi = smi_fn or _read_smi
        #: `() -> {pid: engine_id}` so a process row can name the engine holding it. Injected rather
        #: than imported so this module stays independent of the lifecycle manager.
        self._engine_pids = engine_pids or (lambda: {})
        self._cached = None
        self._cached_at = None

    def snapshot(self, fresh: bool = False) -> dict:
        if not fresh and self._cached is not None and self._cached_at is not None \
                and (self._clock() - self._cached_at) <= self._ttl:
            return self._cached
        snap = self._build()
        self._cached, self._cached_at = snap, self._clock()
        return snap

    def _build(self) -> dict:
        devices, source = self._nvml(), NVML
        if devices is None:
            devices, source = self._smi(), SMI
        if devices is None:
            # An unreadable GPU is a known operating state, not a request failure: one device entry
            # with nulls and an honest `source`.
            devices, source = [{"index": 0}], STATIC

        engine_pids = {}
        try:
            engine_pids = self._engine_pids() or {}
        except Exception:  # noqa: BLE001 — engine attribution is enrichment, never the reading
            engine_pids = {}

        for device in devices:
            for proc in device.get("processes") or []:
                proc["engine_id"] = engine_pids.get(proc.get("pid"))

        return {"observed_at": _iso_now(), "source": source,
                "devices": [_normalize(d, source) for d in devices]}


#: The per-device fields the contract publishes. Listed once so `_normalize` can guarantee every key
#: is PRESENT — a missing key and a null key read very differently to a client, and only one of them
#: says "we looked and could not tell".
_FIELDS = ("index", "name", "uuid", "compute_capability", "total_vram_gb", "free_vram_gb",
           "used_vram_gb", "utilization_pct", "temperature_c")


def _normalize(device: dict, source: str) -> dict:
    out = {field: device.get(field) for field in _FIELDS}
    out["index"] = device.get("index", 0)  # index is always known, even on the static path
    out["processes"] = device.get("processes") or []
    out["source"] = source
    return out


def _iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _read_nvml():
    """Every device, in-process. Returns None when NVML is unavailable so the caller falls back."""
    try:
        import pynvml
    except Exception:  # noqa: BLE001
        return None
    try:
        pynvml.nvmlInit()
    except Exception:  # noqa: BLE001
        return None
    try:
        devices = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            devices.append({
                "index": index,
                "name": _decode(_try(pynvml.nvmlDeviceGetName, handle)),
                "uuid": _decode(_try(pynvml.nvmlDeviceGetUUID, handle)),
                "compute_capability": _capability(pynvml, handle),
                **_memory(pynvml, handle),
                "utilization_pct": _utilization(pynvml, handle),
                "temperature_c": _temperature(pynvml, handle),
                "processes": _processes(pynvml, handle),
            })
        return devices
    except Exception:  # noqa: BLE001 — a partial NVML failure falls back rather than half-reporting
        return None
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — one unsupported query must not lose the whole device
        return None


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _capability(pynvml, handle):
    pair = _try(pynvml.nvmlDeviceGetCudaComputeCapability, handle)
    return f"{pair[0]}.{pair[1]}" if pair else None


def _memory(pynvml, handle):
    info = _try(pynvml.nvmlDeviceGetMemoryInfo, handle)
    if info is None:
        return {"total_vram_gb": None, "free_vram_gb": None, "used_vram_gb": None}
    return {"total_vram_gb": round(info.total / _GIB, 2),
            "free_vram_gb": round(info.free / _GIB, 2),
            "used_vram_gb": round(info.used / _GIB, 2)}


def _utilization(pynvml, handle):
    rates = _try(pynvml.nvmlDeviceGetUtilizationRates, handle)
    return int(rates.gpu) if rates is not None else None


def _temperature(pynvml, handle):
    temp = _try(pynvml.nvmlDeviceGetTemperature, handle, 0)  # 0 == NVML_TEMPERATURE_GPU
    return int(temp) if temp is not None else None


def _processes(pynvml, handle):
    procs = _try(pynvml.nvmlDeviceGetComputeRunningProcesses, handle) or []
    out = []
    for proc in procs:
        used = getattr(proc, "usedGpuMemory", None)
        out.append({"pid": int(proc.pid),
                    "vram_gb": round(used / _GIB, 2) if used else None,
                    "engine_id": None})
    return out


def _read_smi():
    """One `nvidia-smi` fork — the fallback, taken only on a cache miss with NVML unavailable."""
    import subprocess

    query = "index,name,uuid,memory.total,memory.free,memory.used,utilization.gpu,temperature.gpu"
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None

    devices = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        devices.append({
            "index": _int(parts[0]) or 0, "name": parts[1] or None, "uuid": parts[2] or None,
            "compute_capability": None,  # nvidia-smi's gpu query does not carry it
            "total_vram_gb": _mib_to_gb(parts[3]), "free_vram_gb": _mib_to_gb(parts[4]),
            "used_vram_gb": _mib_to_gb(parts[5]),
            "utilization_pct": _int(parts[6]), "temperature_c": _int(parts[7]),
            "processes": [],
        })
    return devices or None


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _mib_to_gb(value):
    parsed = _int(value)
    return round(parsed / 1024.0, 2) if parsed is not None else None
