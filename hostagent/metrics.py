"""Hand-rolled Prometheus exposition (018 US2, FR-174) — stdlib-only, like the supervisors'.

The agent is a direct Prometheus scrape target: GPU/holder/engine/job signals stay observable
when the gateway is down (the pre-018 SPOF, review §4.4). Counters/gauges are a tiny in-process
registry rendered to text-format on demand — no client library in the stdlib-only host process
(same pattern the llama supervisor's /metrics used).
"""
import threading


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._gauges = {}    # (name, labels-tuple) -> value
        self._counters = {}  # (name, labels-tuple) -> value
        self._help = {}      # name -> (type, help)

    def describe(self, name: str, kind: str, help_text: str) -> None:
        self._help[name] = (kind, help_text)

    @staticmethod
    def _key(name, labels):
        return (name, tuple(sorted((labels or {}).items())))

    def set_gauge(self, name: str, value, labels: dict = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def inc(self, name: str, labels: dict = None, by: float = 1.0) -> None:
        with self._lock:
            k = self._key(name, labels)
            self._counters[k] = self._counters.get(k, 0.0) + by

    def counter_value(self, name: str, labels: dict = None) -> float:
        with self._lock:
            return self._counters.get(self._key(name, labels), 0.0)

    def render(self) -> str:
        lines = []
        with self._lock:
            series = [("gauge", self._gauges), ("counter", self._counters)]
            described = set()
            for default_kind, table in series:
                for (name, labels), value in sorted(table.items()):
                    if name not in described:
                        kind, help_text = self._help.get(name, (default_kind, name))
                        lines.append(f"# HELP {name} {help_text}")
                        lines.append(f"# TYPE {name} {kind}")
                        described.add(name)
                    if labels:
                        rendered = ",".join(f'{k}="{v}"' for k, v in labels)
                        lines.append(f"{name}{{{rendered}}} {value}")
                    else:
                        lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


REGISTRY = Registry()
REGISTRY.describe("hostagent_gpu_free_gb", "gauge", "Live free VRAM (TTL-cached NVML read)")
REGISTRY.describe("hostagent_slot_held", "gauge", "1 if the single GPU slot is held")
REGISTRY.describe("hostagent_engine_state", "gauge",
                  "Engine state (one series per engine/state, value 1)")
REGISTRY.describe("hostagent_jobs_active", "gauge", "Jobs currently queued or running")
REGISTRY.describe("hostagent_jobs_interrupted_total", "counter",
                  "Jobs marked interrupted by an agent restart (FR-173 alert)")
REGISTRY.describe("hostagent_wedged", "gauge",
                  "1 if a tenant is wedged (kill failed) and holds the GPU slot")
# 023 US6 (T534, FR-321): bounded-cardinality transport/operation signals — labels are a fixed
# vocabulary (method GET|POST; reason saturated|queue_timeout|too_large), never request-derived.
REGISTRY.describe("hostagent_requests_total", "counter", "HTTP requests, by method")
REGISTRY.describe("hostagent_requests_rejected_total", "counter",
                  "Requests refused at the transport bound (saturated/queue_timeout/too_large)")
REGISTRY.describe("hostagent_request_seconds_sum", "counter",
                  "Total request handling seconds (with _count: average latency)")
REGISTRY.describe("hostagent_request_seconds_count", "counter", "Requests measured for latency")
REGISTRY.describe("hostagent_client_disconnects_total", "counter",
                  "Streams dropped by the client mid-generation")
REGISTRY.describe("hostagent_reload_outcomes_total", "counter",
                  "Serving-LLM reload outcomes (loaded|reloaded|swapped|noop|verify_failed|"
                  "unresolvable|refused)")
REGISTRY.describe("hostagent_disk_free_gb", "gauge",
                  "Free disk on the volume backing the agent state dir (FR-322 low-disk signal)")
