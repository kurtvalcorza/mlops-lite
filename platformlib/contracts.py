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


MONITOR_KINDS = ("input_drift", "quality")
PROMOTION_MODES = ("manual", "suggest", "auto-on-green")
SUGGESTION_STATES = ("open", "accepted", "dismissed", "auto-promoted")


@dataclass
class ModelPolicy(_Base):
    """Per-model loop declaration (018 US3, FR-179; data-model.md §ModelPolicy). Validated at
    write time — an invalid declaration is rejected with a structured error, never stored, and
    never discovered at breach time (spec edge case)."""
    model_name: str = ""
    modality: str = ""
    monitors: list = field(default_factory=list)   # [{"kind": ..., **params}]
    check_interval_s: int = 900
    on_breach: dict = field(default_factory=lambda: {"action": "retrain", "dataset": "latest",
                                                     "params": {}})
    promotion_mode: str = "manual"
    enabled: bool = True
    updated_at: float = 0.0
    updated_by: str = ""

    def validate(self):
        from .topology import TRAINABLE_MODALITIES

        errors = []
        if not self.model_name:
            errors.append({"field": "model_name", "reason": "required"})
        if self.modality not in TRAINABLE_MODALITIES:
            errors.append({"field": "modality",
                           "reason": f"must be one of {TRAINABLE_MODALITIES} "
                                     f"(a modality with a fine-tune flow)"})
        if not isinstance(self.check_interval_s, int) or self.check_interval_s < 60:
            errors.append({"field": "check_interval_s", "reason": "must be an integer >= 60"})
        if not isinstance(self.enabled, bool):
            # Codex round 6 (018): `enabled: "false"` stored as-is is TRUTHY in tick() — a policy
            # the operator meant to pause would keep checking and retraining.
            errors.append({"field": "enabled", "reason": "must be a boolean (true/false)"})
        if not self.monitors:
            errors.append({"field": "monitors", "reason": "at least one monitor is required"})
        for i, mon in enumerate(self.monitors):
            if not isinstance(mon, dict) or mon.get("kind") not in MONITOR_KINDS:
                errors.append({"field": f"monitors[{i}].kind",
                               "reason": f"must be one of {MONITOR_KINDS}"})
                continue
            if mon["kind"] == "input_drift":
                ref = mon.get("reference")
                if (not isinstance(ref, dict) or not ref.get("name")
                        or not ref.get("version")):
                    errors.append({"field": f"monitors[{i}].reference",
                                   "reason": "input_drift needs a reference dataset object "
                                             "with both 'name' and 'version'"})
            # Numeric knobs are checked at WRITE time (Codex round 5, 018): the scheduler casts
            # them with int()/float() at check time — a malformed value would fail every due
            # tick as a "check error" and the policy would never evaluate or retrain, exactly
            # the discovered-at-breach-time failure FR-179 exists to prevent.
            for key, cast, ok_fn, want in (
                    ("window_n", int, lambda v: v > 0, "a positive integer"),
                    ("drop_pct", float, lambda v: v >= 0, "a number >= 0"),
                    ("threshold", float, lambda v: v > 0, "a number > 0"),
                    ("baseline", float, lambda v: True, "a number")):
                if mon.get(key) is None:
                    continue
                try:
                    ok = ok_fn(cast(mon[key]))
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    errors.append({"field": f"monitors[{i}].{key}",
                                   "reason": f"must be {want}"})
        if not isinstance(self.on_breach, dict) or self.on_breach.get("action") != "retrain":
            errors.append({"field": "on_breach.action", "reason": "must be 'retrain'"})
        elif not self.on_breach.get("dataset"):
            errors.append({"field": "on_breach.dataset",
                           "reason": "required — 'latest' or a pinned dataset version; the "
                                     "dataset name comes from on_breach.params.dataset_name"})
        else:
            params = self.on_breach.get("params") or {}
            if not isinstance(params, dict):
                # Codex round 5 (018): a truthy non-object here crashed the .get() below with an
                # AttributeError — an unstructured 500 where FR-179 promises a structured 400.
                errors.append({"field": "on_breach.params",
                               "reason": "must be an object of retrain parameters"})
            elif not params.get("dataset_name"):
                errors.append({"field": "on_breach.params.dataset_name", "reason": "required"})
            elif (params.get("output_name") or self.model_name) != self.model_name:
                # Codex round 5 (018): the loop gates and promotes policy.model_name with only
                # the run's returned VERSION — versions are per-model, so registering under any
                # other name makes that pair meaningless (blocked as missing, or promoting an
                # unrelated version). The launch path also forces output_name, as the belt.
                errors.append({"field": "on_breach.params.output_name",
                               "reason": f"must equal the policy model ({self.model_name!r}) — "
                                         f"the loop gates/promotes that model by returned "
                                         f"version"})
        if self.promotion_mode not in PROMOTION_MODES:
            errors.append({"field": "promotion_mode",
                           "reason": f"must be one of {PROMOTION_MODES}"})
        if errors:
            raise ContractError(json.dumps({"errors": errors}))


@dataclass
class PendingRetrain(_Base):
    """The queue-of-one parked retrain per model (FR-182; data-model.md). Durable beside the
    policies so a gateway restart resumes the retry (clarify Q2 remediation U2)."""
    model_name: str = ""
    breach: dict = field(default_factory=dict)     # {"signal", "score", "at"}
    attempts: int = 0
    next_attempt_at: float = 0.0

    def validate(self):
        self._require("model_name")


@dataclass
class PromotionSuggestion(_Base):
    """A gate-passing candidate awaiting the operator (mode `suggest`), or the audit record of an
    automatic promotion (mode `auto-on-green`) — data-model.md §PromotionSuggestion."""
    id: str = ""
    model_name: str = ""
    candidate_version: str = ""
    gate_verdict: dict = field(default_factory=dict)
    shadow_verdict: Optional[dict] = None
    state: str = "open"
    created_at: float = 0.0
    resolved_at: Optional[float] = None
    actor: Optional[str] = None

    def validate(self):
        self._require("id", "model_name", "candidate_version")
        if self.state not in SUGGESTION_STATES:
            raise ContractError(f"PromotionSuggestion.state {self.state!r} "
                                f"not in {SUGGESTION_STATES}")


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
