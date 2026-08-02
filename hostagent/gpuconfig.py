"""Typed coordinator tunables (026 T623 — `.specify/memory/hardware-profile.md` §GPU admission).

Every quantity `contracts/admission-scheduler.md` names as a tunable resolves from here, and none is
hardcoded at a use site. That is the whole point of the module: the hardware profile is the single
machine-specific file, so retargeting the platform to a different GPU must not mean grepping the
coordinator for magic numbers.

Defaults mirror the profile's table. Each is overridable by an environment variable so a drill can
tighten a timeout without editing the profile, and the profile stays the documentation of what the
*machine* wants rather than what a particular test run wanted.
"""
import os
from dataclasses import dataclass

_GIB = 1024 ** 3

#: Hardware-profile defaults (RTX 5070 Ti Laptop, 12 GiB). Units are bytes/seconds internally —
#: the profile states GB and s, and the conversion happens once, here.
_DEFAULTS = {
    "safety_reserve_bytes": 1.0 * _GIB,
    "safety_headroom_bytes": 0.5 * _GIB,
    "max_admission_attempts": 3,
    "drain_timeout_s": 30.0,
    "job_drain_timeout_s": 120.0,
    "admission_backoff_base_s": 0.25,
    "admission_backoff_cap_s": 5.0,
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


@dataclass(frozen=True)
class CoordinatorConfig:
    """The coordinator's admission tunables, resolved once.

    Frozen because the coordinator reads these inside its critical sections: a value that could
    change between the two bounds of one admission decision would make the decision incoherent
    (stage 1 could fit a load that stage 3 then rejects for a reason that did not exist when the
    reservation was recorded).
    """

    #: Held back from the device total. Covers driver/context overhead and unaccounted external GPU
    #: consumers — on a desktop GPU with a display attached, not safely reducible below 1 GiB.
    safety_reserve_bytes: float = _DEFAULTS["safety_reserve_bytes"]

    #: Slack required ABOVE an incoming load's estimate when checking it against live-free. This is
    #: the second bound's margin, and is distinct from `safety_reserve_bytes`, which shrinks the
    #: budget in the first bound. Conflating them was the v1 double-count.
    safety_headroom_bytes: float = _DEFAULTS["safety_headroom_bytes"]

    #: Bounded reserve→evict→retry cycles before refusing `gpu_busy`. Bounded, never a spin: no
    #: admission path may wait unbounded on another tenant's in-flight request.
    max_admission_attempts: int = _DEFAULTS["max_admission_attempts"]

    #: Max wait for ONE victim's in-flight requests to finish before its eviction is abandoned.
    drain_timeout_s: float = _DEFAULTS["drain_timeout_s"]

    #: Max wait for the WHOLE serving set to empty before an exclusive job gives up its barrier.
    #: Deliberately much larger than `drain_timeout_s` — a job drains every resident, not one, and
    #: `evict()`'s barrier-aware revert hands stalled victims to this longer budget.
    job_drain_timeout_s: float = _DEFAULTS["job_drain_timeout_s"]

    #: Exponential jittered backoff between admission attempts, capped.
    admission_backoff_base_s: float = _DEFAULTS["admission_backoff_base_s"]
    admission_backoff_cap_s: float = _DEFAULTS["admission_backoff_cap_s"]

    #: `usable_capacity = min(configured_budget, NVML_total - safety_reserve)`. None means "no
    #: configured budget" — the device total minus the reserve is then the only bound.
    configured_budget_bytes: float = None

    def backoff_for(self, attempt: int, jitter: float = None) -> float:
        """Exponential, jittered, capped backoff for a 1-based attempt number.

        Jitter is multiplicative in [0.5, 1.0] so that N callers refused by the same eviction do not
        re-enter stage 1 in lockstep — synchronized retries would rebuild the exact contention that
        refused them. `jitter` is injectable so tests are deterministic.
        """
        raw = self.admission_backoff_base_s * (2 ** max(0, attempt - 1))
        capped = min(raw, self.admission_backoff_cap_s)
        if jitter is None:
            import random
            jitter = random.uniform(0.5, 1.0)
        return capped * jitter

    def usable_capacity(self, device_total_bytes: float) -> float:
        """`min(configured_budget, device_total − safety_reserve)` — invariant 1's right-hand side.

        Never negative: a device smaller than the reserve yields 0 (admit nothing) rather than a
        negative capacity that would make every comparison read as "fits".
        """
        from_device = device_total_bytes - self.safety_reserve_bytes
        if self.configured_budget_bytes is not None:
            from_device = min(from_device, self.configured_budget_bytes)
        return max(0.0, from_device)


def load() -> CoordinatorConfig:
    """Resolve the config from the environment, falling back to the hardware profile's defaults."""
    budget_gb = os.getenv("GPU_VRAM_BUDGET_GB")
    configured = float(budget_gb) * _GIB if budget_gb and budget_gb.strip() else None
    return CoordinatorConfig(
        safety_reserve_bytes=_env_float("GPU_SAFETY_RESERVE_GB",
                                        _DEFAULTS["safety_reserve_bytes"] / _GIB) * _GIB,
        safety_headroom_bytes=_env_float("GPU_SAFETY_HEADROOM_GB",
                                         _DEFAULTS["safety_headroom_bytes"] / _GIB) * _GIB,
        max_admission_attempts=_env_int("GPU_MAX_ADMISSION_ATTEMPTS",
                                        _DEFAULTS["max_admission_attempts"]),
        drain_timeout_s=_env_float("GPU_DRAIN_TIMEOUT_S", _DEFAULTS["drain_timeout_s"]),
        job_drain_timeout_s=_env_float("GPU_JOB_DRAIN_TIMEOUT_S",
                                       _DEFAULTS["job_drain_timeout_s"]),
        admission_backoff_base_s=_env_float("GPU_ADMISSION_BACKOFF_BASE_S",
                                            _DEFAULTS["admission_backoff_base_s"]),
        admission_backoff_cap_s=_env_float("GPU_ADMISSION_BACKOFF_CAP_S",
                                           _DEFAULTS["admission_backoff_cap_s"]),
        configured_budget_bytes=configured,
    )
