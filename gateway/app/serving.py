"""Serving client + GPU-state surface (T016/T017; 008 US1/US3).

The gateway proxies inference to the native **GPU host agent** on the WSL host (018). The agent
owns each engine's lifecycle (on-demand load, idle VRAM release, oversize rejection) behind its
`/engines/llm` byte-compatible surface.

**Principle II authority is the agent's** (008 Claude review F5, generalized at 018): the single
race-free **admission slot** the agent holds is what guarantees one GPU tenant at a time — **not**
this gateway. The `_gpu_lock` below is only a gateway-side concurrency limiter (at most one
in-flight agent call); it does not *enforce* Principle II. `gpu_state()` reads the GPU holder via
the agent's per-engine `/health` for the UI status line (FR-068).
"""
import asyncio

import httpx

from . import settings

SERVING_URL = settings.SERVING_URL
SERVING_MODEL = settings.SERVING_MODEL

# Admission tenant id -> the label the UI status line shows (008 FR-068). The agent reports its
# single admission holder in /health (T364: sourced from `admission.holder()`, no longer a shared
# lockfile), so this one read covers whichever tenant (llm, vision, asr, training) holds the GPU.
# The ids already equal the labels; the map stays for a stable display contract + future aliases.
_HOLDER_LABEL = {"llm": "llm", "vision": "vision", "training": "training", "asr": "asr"}

_gpu_lock = asyncio.Lock()  # gateway-side concurrency limiter only; Principle II is the agent's job


class ServingError(Exception):
    """Serving backend unreachable or errored."""


class ModelTooLargeError(ServingError):
    """Requested model exceeds the VRAM budget (FR-004)."""


class ServingBusyError(ServingError):
    """Another GPU tenant holds the single slot — the refuse-if-held path (Principle II / 008).
    The host agent (018 T358) returns 409 for this; the retired supervisor lumped it into 507. It
    is contention, NOT a backend fault, so the router classifies it as `rejected` (not `error`)
    and surfaces it as a 409 rather than a 502."""


async def health() -> bool:
    async with httpx.AsyncClient(headers=settings.agent_headers(), timeout=5) as client:
        try:
            r = await client.get(f"{SERVING_URL}/health")
            return r.status_code == 200 and r.json().get("ok", False)
        except httpx.HTTPError:
            return False


def _identity_from_health(h: dict) -> dict:
    """The served-LLM identity fields from the agent's /engines/llm/health payload (022 US2,
    contracts/agent-identity-and-allowlist.md). Pure so the honest-identity mapping unit-tests
    without HTTP. The agent is the ONLY component that knows what is resident — nothing here
    falls back to the fixed SERVING_MODEL config (that fallback WAS the live divergence bug)."""
    return {
        "serving_model": h.get("model_name") or h.get("model") or "unknown",
        "serving_version": h.get("registry_version"),
        "base": h.get("base"),
        "adapter": h.get("adapter"),
    }


_UNKNOWN_IDENTITY = {"serving_model": "unknown", "serving_version": None,
                     "base": None, "adapter": None}


async def gpu_state() -> dict:
    """GPU state for the UI status line (008 FR-068): which tenant holds the single GPU slot, the
    serving model identity, and whether the LLM is resident.

    Sourced from the agent's per-engine `/health` (`lease_holder`, from `admission.holder()` since
    T364) — so one read reflects whichever tenant (llm, vision, asr, training) holds the GPU.
    022 (FR-260): the serving model+version are the AGENT-REPORTED identity, degrading to
    `unknown` when the agent is unreachable — never a stale config guess (T470). Key-free: the
    BFF contract is unchanged (no key reaches the browser). holder=None when unreadable.
    """
    holder, resident, ident = None, False, dict(_UNKNOWN_IDENTITY)
    async with httpx.AsyncClient(headers=settings.agent_headers(), timeout=5) as client:
        try:
            r = await client.get(f"{SERVING_URL}/health")
            if r.status_code in (200, 503):  # 503 = engine unavailable, but the payload is honest
                h = r.json()
                resident = bool(h.get("resident"))
                holder = _HOLDER_LABEL.get(h.get("lease_holder"), h.get("lease_holder"))
                ident = _identity_from_health(h)
        except (httpx.HTTPError, ValueError):
            pass
    return {"holder": holder, "resident": resident, **ident}


async def llm_identity() -> dict:
    """The agent-reported served-LLM identity {serving_model, serving_version, base, adapter} —
    what prediction logging attributes each served prediction to (022 US2, FR-261: the quality
    window must key on the model+version that actually produced the output). `unknown`/None when
    the agent is unreachable — a prediction is never logged under a config guess."""
    async with httpx.AsyncClient(headers=settings.agent_headers(), timeout=5) as client:
        try:
            r = await client.get(f"{SERVING_URL}/health")
            if r.status_code in (200, 503):
                return _identity_from_health(r.json())
        except (httpx.HTTPError, ValueError):
            pass
    return dict(_UNKNOWN_IDENTITY)


def resident_identity() -> dict:
    """SYNC agent-reported resident identity {serving_model, serving_version, ..., resident} for
    the activation verifier/reconciler (023 US5, FR-312) — they run in the threadpool, not the
    event loop. Same honesty rule as llm_identity(): unknown when unreachable, never a config
    guess."""
    try:
        with httpx.Client(headers=settings.agent_headers(), timeout=5) as client:
            r = client.get(f"{SERVING_URL}/health")
            if r.status_code in (200, 503):
                h = r.json()
                return {**_identity_from_health(h), "resident": bool(h.get("resident"))}
    except (httpx.HTTPError, ValueError):
        pass
    return {**_UNKNOWN_IDENTITY, "resident": False}


def request_llm_reload(preempt: bool = False, *, operation_id: str = None,
                       target: dict = None) -> dict:
    """Ask the agent to make the newly selected serving-LLM live NOW (022 US1, FR-255 — the
    gated promote is the go-live action; this is its reload half). Synchronous (the promote
    handler runs in the threadpool). Never raises: the alias + pointer have already moved, so a
    refused/failed reload is reported as status=deferred|unreachable with the agent's reason —
    FR-259's refuse/defer vocabulary — for the operator to see and retry (re-promoting the same
    version re-requests the reload idempotently).

    023 US5 (FR-307/308/312): `operation_id` + `target` {model_name, version} key the reload to
    the durable ActivationOperation — the agent replays a completed same-target operation instead
    of reloading twice, rejects a different target under the same id, and accepts success only
    with the EXACT target resident."""
    headers = {}
    if settings.AGENT_CONTROL_SECRET:
        headers["X-Agent-Control"] = settings.AGENT_CONTROL_SECRET
    payload = {"preempt": preempt}
    if operation_id:
        payload["operation_id"] = operation_id
        payload["target"] = target or {}
    try:
        with httpx.Client(headers=settings.agent_headers(), timeout=300) as client:
            r = client.post(f"{settings.AGENT_URL}/control/reload",
                            json=payload, headers=headers)
    except httpx.HTTPError as e:
        return {"status": "unreachable",
                "reason": f"agent unreachable at {settings.AGENT_URL}: {e} — the switch is "
                          f"picked up on the next cold load"}
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200:
        return body  # loaded | reloaded | swapped | noop (+ the served identity)
    return {"status": "deferred",
            "reason": body.get("error") or f"agent refused the reload ({r.status_code})",
            # FR-265: the agent distinguishes an UNLOADABLE target (roll the pointer back) from a
            # retryable job-holder/confirm deferral — the promote route reads this to decide.
            "unresolvable": bool(body.get("unresolvable"))}


async def run_inference(prompt: str, max_tokens: int = 256, temperature: float = 0.7, *,
                        preempt: bool = False) -> dict:
    """Serialized call to the agent; returns {text, load_ms, infer_ms, model, usage}. `preempt=true`
    (T363) appends `?preempt=true` so the AGENT orchestrates the swap (evict a resident serving
    holder, refuse a `kind="job"` holder) — the gateway no longer brokers it."""
    url = f"{SERVING_URL}/infer" + ("?preempt=true" if preempt else "")
    async with _gpu_lock:
        async with httpx.AsyncClient(headers=settings.agent_headers(), timeout=300) as client:
            try:
                r = await client.post(
                    url,
                    json={"prompt": prompt, "max_tokens": max_tokens, "temperature": temperature},
                )
            except httpx.HTTPError as e:
                raise ServingError(f"serving supervisor unreachable at {SERVING_URL}: {e}")
    if r.status_code == 507:
        raise ModelTooLargeError(r.json().get("error", "model exceeds VRAM budget"))
    if r.status_code == 409:
        # 018 T358: the agent returns 409 when another GPU tenant holds the slot. Keep it a distinct
        # "busy" (not a generic ServingError → 502) so /infer reports it rejected, keeping the
        # pre-018 refuse-if-held client semantics the retired supervisor expressed as a 507.
        try:
            detail = r.json().get("error") or "GPU busy — another tenant holds the slot"
        except Exception:
            detail = "GPU busy — another tenant holds the slot"
        raise ServingBusyError(detail)
    if r.status_code != 200:
        raise ServingError(f"serving error {r.status_code}: {r.text[:200]}")
    return r.json()
