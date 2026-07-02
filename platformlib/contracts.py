"""Typed payloads exchanged between the gateway and the host agent (018 T343, FR-176).

Stdlib dataclasses + explicit `validate()` (research R2) — the ONLY shapes the two runtimes
exchange. Parsing ignores unknown fields (forward compatibility); missing/invalid required fields
raise `ContractError`. Additive evolution only within 018 (contracts/platformlib.md).
"""
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Optional


class ContractError(ValueError):
    """A payload does not satisfy its contract (missing/invalid field)."""


def _from_json(cls, data):
    """Build `cls` from a dict/JSON string, ignoring unknown fields; then validate()."""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ContractError(f"{cls.__name__}: expected an object, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    obj = cls(**{k: v for k, v in data.items() if k in known})
    obj.validate()
    return obj


class _Base:
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data):
        return _from_json(cls, data)

    def validate(self) -> None:  # overridden where there are constraints
        return None

    def _require(self, *names) -> None:
        for n in names:
            if getattr(self, n) in (None, ""):
                raise ContractError(f"{type(self).__name__}.{n} is required")


JOB_KINDS = ("train", "hpo", "batch", "shadow")
JOB_STATES = ("queued", "running", "succeeded", "failed", "interrupted")
ENGINE_STATES = ("disabled", "unavailable", "cold", "loading", "ready",
                 "draining", "idle-releasing", "wedged")


@dataclass
class EngineState(_Base):
    """One engine's row in the agent's /engines listing (data-model.md §EngineAdapter)."""
    engine_id: str = ""
    state: str = "cold"
    gpu: bool = False
    optional: bool = False
    reason: Optional[str] = None       # set when state == "unavailable"/"wedged"

    def validate(self):
        self._require("engine_id")
        if self.state not in ENGINE_STATES:
            raise ContractError(f"EngineState.state {self.state!r} not in {ENGINE_STATES}")


@dataclass
class AgentHealth(_Base):
    """`GET /health` on the agent (contracts/agent-api.md). Served from cache — never forks."""
    ok: bool = True
    engines: dict = field(default_factory=dict)     # engine_id -> state string
    gpu_free_gb: Optional[float] = None
    holder: Optional[str] = None                    # tenant id or None
    holder_kind: Optional[str] = None               # "serving" | "job" | None
    wedged: bool = False
    jobs_active: int = 0
    interrupted_since_start: int = 0


@dataclass
class AdmissionRequest(_Base):
    """Ask the agent's admission for the single GPU slot (FR-168)."""
    tenant: str = ""
    kind: str = "serving"                           # "serving" | "job"
    est_gb: float = 0.0

    def validate(self):
        self._require("tenant")
        if self.kind not in ("serving", "job"):
            raise ContractError(f"AdmissionRequest.kind {self.kind!r} invalid")
        if self.est_gb < 0:
            raise ContractError("AdmissionRequest.est_gb must be >= 0")


@dataclass
class AdmissionResult(_Base):
    admitted: bool = False
    holder: Optional[str] = None                    # who holds the slot when refused
    reason: Optional[str] = None                    # "held" | "vram" | None


@dataclass
class SwapCommand(_Base):
    """Operator-confirmed preemptive swap intent (FR-171): evict → free → load, transactional."""
    target: str = ""
    drain_timeout_s: float = 10.0

    def validate(self):
        self._require("target")
        if self.drain_timeout_s < 0:
            raise ContractError("SwapCommand.drain_timeout_s must be >= 0")


@dataclass
class UnloadResult(_Base):
    status: str = "idle"                            # "unloaded" | "idle" | "busy"
    drained: Optional[bool] = None
    detail: Optional[str] = None

    def validate(self):
        if self.status not in ("unloaded", "idle", "busy"):
            raise ContractError(f"UnloadResult.status {self.status!r} invalid")


@dataclass
class JobSubmit(_Base):
    """`POST /jobs` body (contracts/agent-api.md)."""
    kind: str = ""
    modality: str = ""
    request: dict = field(default_factory=dict)

    def validate(self):
        self._require("kind", "modality")
        if self.kind not in JOB_KINDS:
            raise ContractError(f"JobSubmit.kind {self.kind!r} not in {JOB_KINDS}")


@dataclass
class JobRecord(_Base):
    """Durable job state (data-model.md §JobRecord; journal pre-US4, `jobs` table after)."""
    job_id: str = ""
    kind: str = ""
    modality: str = ""
    request: dict = field(default_factory=dict)
    state: str = "queued"
    submitted_at: float = 0.0
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    result: Optional[dict] = None
    reason: Optional[str] = None                    # failure/interruption reason

    def validate(self):
        self._require("job_id", "kind")
        if self.kind not in JOB_KINDS:
            raise ContractError(f"JobRecord.kind {self.kind!r} not in {JOB_KINDS}")
        if self.state not in JOB_STATES:
            raise ContractError(f"JobRecord.state {self.state!r} not in {JOB_STATES}")
