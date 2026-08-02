"""OpenAI-compatible tenant inference surface (026 T625, T627, T632, T655, T681).

The LAN front door. Every route here authenticates a **tenant** (not the operator), reserves
GPU-seconds before the work, maps to an existing engine child, and settles to actual afterwards.

## Why an adapter and not a new engine

The platform's existing children already cover chat, embeddings, ASR, and vision. What tenants lack
is a surface their standard clients can talk to without a custom SDK, so this module is an
**interface adapter** over `/infer`, `/embed`, `/transcribe`, and the vision verbs — not a second
serving path. Principle V: the OpenAI shape is an interface, and swapping it costs nothing behind it.

## Metering, and the one place the contract splits

**Non-streaming**: the work finishes before headers are written, so `X-GPU-Seconds` (settled) and
`X-Quota-Remaining` are both final and both sent.

**Streaming** (T681): headers are flushed *before* the SSE body, and therefore before this request's
GPU-seconds exist. `X-GPU-Seconds` is consequently **not** sent on a streamed response — filling it
would mean either buffering the whole completion (defeating streaming) or labelling an estimate as
settled usage. Final usage instead arrives as a terminal `usage` SSE event immediately before
`[DONE]`. `X-Quota-Remaining` may still be sent, documented as the value *at admission time*.

## Task-typed CV (T655)

`/v1/vision/{classify,detect}` is deliberately not OpenAI-shaped: no OpenAI schema exists for
detection, and inventing one would mean tenants writing to a fake standard. Task-typed by design
(research R2).
"""
import json
import time

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from ..broker import refuse
from ..metering import quota_state, release, reserve_or_refuse, settle
from ..settings import ASR_URL, BENTO_URL, EMBED_URL, SERVING_URL, agent_headers
from ..tenancy import require_tenant

router = APIRouter(prefix="/v1")

BROKER_REQUESTS = Counter("broker_requests_total", "Broker inference requests",
                          ["modality", "status"])
#: Settled GPU-seconds by modality. Deliberately **not** labelled by tenant: tenant is an identifier,
#: so a label would mint a permanent time series per tenant the broker ever served, and the platform's
#: metrics contract bounds label cardinality for exactly that reason. Per-tenant consumption is a
#: ledger question and is answered by `GET /admin/usage`, which reads the rows the quota is actually
#: enforced against rather than a parallel counter that could drift from them.
BROKER_GPU_SECONDS = Counter("broker_gpu_seconds_total", "Settled GPU-seconds", ["modality"])
BROKER_LATENCY = Histogram("broker_request_latency_seconds", "Broker request latency", ["modality"])


# -- agent refusal mapping ---------------------------------------------------------------------------

def _map_agent_refusal(status: int, body: str, *, model: str = "") -> None:
    """Translate a child/agent refusal into the broker's one-code-one-status vocabulary (T688).

    The agent speaks the pre-broker dialect: 409 for "another GPU tenant holds the lease", 507 for
    "live VRAM cannot admit this model". Both are **contention** from a tenant's point of view, and
    both become `503 gpu_busy` with `Retry-After` — the tenant should retry, and the earlier
    revisions that surfaced these as 409/429/413 gave clients three codes for one condition.

    `413 model_too_large` is reserved for the coordinator's *permanent* verdict, which it signals
    explicitly; it is never inferred from a contention status.
    """
    if status in (409, 503):
        raise refuse("gpu_busy", f"the GPU is busy: {body[:200]}")
    if status == 507:
        # 507 is the legacy "cannot fit right now". Without the coordinator's verdict we cannot know
        # whether it would fit an empty GPU, and the safe direction is the retryable one: telling a
        # client to give up on a request that would have succeeded seconds later is the worse error.
        raise refuse("gpu_busy", f"the model could not be placed right now: {body[:200]}")
    if status == 413:
        raise refuse("model_too_large",
                     f"{model or 'the model'} does not fit this GPU even when empty: {body[:200]}")
    raise refuse("store_unavailable", f"serving backend error {status}: {body[:200]}")


class _Meter:
    """Reserve on enter, settle on exit — including the error and disconnect paths.

    A context manager rather than a decorator because the settled value is not known until the body
    finishes, and because the release-on-failure has to happen on **every** exit. An un-settled
    reservation holds quota against its tenant until something reconciles it, so "we forgot to
    settle" is a wrong bill, not a missing log line.
    """

    def __init__(self, request: Request, tenant: dict, modality: str, est: float = None):
        self.op_id = getattr(request.state, "op_id", None) or f"req-{id(request)}"
        self.tenant = tenant
        self.modality = modality
        self.est = est
        self.started = None
        self.gpu_seconds = 0.0
        self.settled = None
        #: True once a streaming generator has taken over settlement — see `transfer()`.
        self.transferred = False

    def __enter__(self):
        reserve_or_refuse(self.op_id, self.tenant, self.est, kind="inference",
                          modality=self.modality)
        self.started = time.monotonic()
        return self

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - (self.started or time.monotonic()))

    def transfer(self) -> "_Meter":
        """Hand settlement ownership to a streaming generator, so `__exit__` does not settle.

        **This is what makes streamed metering correct.** A `return StreamingResponse(...)` from
        inside `with meter:` runs `__exit__` at the moment the *response object* is constructed —
        before the generator has opened the upstream stream or produced a single token. The meter
        therefore settled at ~0 GPU-seconds, and the generator's later `finish()` was a no-op
        because `finish` is idempotent once `settled` is set. Every streamed request was materially
        undercharged, and the terminal usage event reported that premature value as fact.

        The reservation is already placed by `__enter__`; only the *settlement* moves. If the
        generator is never consumed, its `aclose()` still runs the `finally` — Starlette closes the
        response body — so the reservation is not orphaned.
        """
        self.transferred = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and self.started is None:
            return False
        if exc_type is not None:
            # The work failed. Charge what the GPU actually spent rather than releasing in full: a
            # failure after the model was loaded and tokens were generated still consumed the GPU,
            # and refunding it would let a tenant burn the device for free by cancelling.
            #
            # This runs even when transferred: the failure happened before the generator existed, so
            # there is nothing downstream that will settle it.
            self.finish(self.elapsed())
            return False
        if self.transferred:
            return False  # the generator settles when the stream actually ends
        if self.settled is None:
            self.finish(self.elapsed())
        return False

    def finish(self, gpu_seconds: float) -> dict:
        """Settle once. Idempotent at this level so a route that settles explicitly (the streaming
        path, which knows its own end) does not settle twice through `__exit__`."""
        if self.settled is not None:
            return self.settled
        self.gpu_seconds = float(gpu_seconds)
        self.settled = settle(self.op_id, self.gpu_seconds)
        BROKER_GPU_SECONDS.labels(modality=self.modality).inc(self.gpu_seconds)
        return self.settled

    def abandon(self) -> None:
        """Release in full — the work never reached the GPU (an admission refusal)."""
        if self.settled is None:
            release(self.op_id)
            self.settled = {"settled": True, "deferred": False, "gpu_seconds": 0.0}

    def headers(self, *, streaming: bool = False) -> dict:
        """Response headers. `X-GPU-Seconds` is omitted on a streamed response — see T681 and the
        module docstring: at header-flush time the value does not yet exist."""
        state = quota_state(self.tenant["id"])
        headers = {}
        remaining = state.get("remaining_gpu_seconds")
        if remaining is not None:
            headers["X-Quota-Remaining"] = f"{remaining:.3f}"
        if not streaming:
            headers["X-GPU-Seconds"] = f"{self.gpu_seconds:.3f}"
        headers["X-Tenant"] = self.tenant["name"]
        return headers


async def _post(url: str, payload: dict, timeout: float = 300.0):
    async with httpx.AsyncClient(headers=agent_headers(), timeout=timeout) as client:
        try:
            return await client.post(url, json=payload)
        except httpx.HTTPError as e:
            raise refuse("store_unavailable", f"serving backend unreachable: {e}")


# -- models ------------------------------------------------------------------------------------------

@router.get("/models")
async def list_models(tenant: dict = Depends(require_tenant)):
    """OpenAI-shaped model listing, so an unmodified client can discover what it may ask for.

    Only models with a **promoted serving version** are listed: a registered model with nothing
    promoted is not something a tenant can request, and listing it would advertise a name every
    request against it would then refuse.
    """
    from .. import registry

    try:
        models = [m for m in registry.list_models() if m.get("serving_version")]
    except Exception:  # noqa: BLE001 — a registry outage degrades the listing, never the surface
        models = []
    return {"object": "list",
            "data": [{"id": m["name"], "object": "model", "owned_by": "mlops-lite",
                      "version": m["serving_version"]} for m in models]}


# -- chat completions ---------------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    max_tokens: int = 256
    temperature: float = 0.7
    stream: bool = False


def _flatten(messages: list) -> str:
    """Collapse an OpenAI message list into the single prompt the llm child takes.

    A chat template belongs to the model, and the child already applies the one its model wants, so
    doing a second templating pass here would double-wrap the prompt. This is a faithful, minimal
    linearization — role-tagged so a model that was trained on tagged turns still sees them.
    """
    parts = []
    for m in messages:
        role = (m.role or "user").strip()
        if role == "system":
            parts.append(m.content)
        else:
            parts.append(f"{role}: {m.content}")
    parts.append("assistant:")
    return "\n".join(p for p in parts if p)


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           tenant: dict = Depends(require_tenant)):
    """OpenAI-compatible chat completion. `stream: true` returns SSE with a terminal usage event."""
    if not body.messages:
        raise refuse("forbidden", "messages must not be empty")
    prompt = _flatten(body.messages)
    meter = _Meter(request, tenant, "chat")
    with BROKER_LATENCY.labels(modality="chat").time():
        with meter:
            if body.stream:
                # `transfer()` before returning: the generator, not this context, owns settlement.
                # Without it `__exit__` settles here — before a token exists — and the generator's
                # later `finish()` is a no-op.
                return await _chat_stream(prompt, body, meter.transfer())
            r = await _post(f"{SERVING_URL}/infer",
                            {"prompt": prompt, "max_tokens": body.max_tokens,
                             "temperature": body.temperature})
            if r.status_code != 200:
                meter.abandon()
                BROKER_REQUESTS.labels(modality="chat", status="refused").inc()
                _map_agent_refusal(r.status_code, r.text, model=body.model)
            payload = r.json()
            meter.finish(meter.elapsed())
            BROKER_REQUESTS.labels(modality="chat", status="ok").inc()
            text = payload.get("completion") or payload.get("text") or ""
            return Response(
                content=json.dumps({
                    "id": meter.op_id, "object": "chat.completion", "created": int(time.time()),
                    "model": payload.get("serving_model") or body.model,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": payload.get("prompt_tokens", 0),
                              "completion_tokens": payload.get("completion_tokens", 0),
                              "total_tokens": payload.get("total_tokens", 0),
                              "gpu_seconds": meter.gpu_seconds}}),
                media_type="application/json", headers=meter.headers())


async def _chat_stream(prompt: str, body: ChatCompletionRequest, meter: "_Meter"):
    """SSE streaming with the terminal usage event (T681).

    The usage event is emitted from a `finally` so it survives a client disconnect mid-stream: the
    GPU-seconds were spent either way, and a disconnect must not turn into an unsettled reservation
    that holds the tenant's quota until something reconciles it.
    """
    # Set only when the upstream stream completed normally. The `finally` used to increment
    # `status="ok"` unconditionally, so an upstream refusal — which returns early after yielding an
    # SSE error — was counted as a success. A refusal rate computed from that counter would read as
    # zero no matter how often the GPU turned tenants away.
    outcome = {"status": "refused"}

    async def gen():
        try:
            async with httpx.AsyncClient(headers=agent_headers(), timeout=600.0) as client:
                async with client.stream(
                        "POST", f"{SERVING_URL}/infer/stream",
                        json={"prompt": prompt, "max_tokens": body.max_tokens,
                              "temperature": body.temperature}) as upstream:
                    if upstream.status_code != 200:
                        text = (await upstream.aread()).decode("utf-8", "replace")
                        yield _sse_error(upstream.status_code, text)
                        return
                    outcome["status"] = "ok"
                    async for chunk in upstream.aiter_lines():
                        if not chunk:
                            continue
                        yield ("data: " + json.dumps({
                            "id": meter.op_id, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": body.model,
                            "choices": [{"index": 0, "delta": {"content": _delta(chunk)}}],
                        }) + "\n\n")
        finally:
            # THE settlement for a streamed request. `elapsed()` here spans the actual stream,
            # because this runs when the generator finishes — normally, on error, or on a client
            # disconnect (Starlette closes the body, which raises GeneratorExit into this frame).
            meter.finish(meter.elapsed())
            state = quota_state(meter.tenant["id"])
            usage = {"gpu_seconds": round(meter.gpu_seconds, 3),
                     "quota_remaining": state.get("remaining_gpu_seconds")}
            window_start = state.get("window_start")
            if window_start is not None:
                usage["window_start"] = getattr(window_start, "isoformat", lambda: window_start)()
            yield "event: usage\ndata: " + json.dumps(usage) + "\n\n"
            yield "data: [DONE]\n\n"
            BROKER_REQUESTS.labels(modality="chat", status=outcome["status"]).inc()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=meter.headers(streaming=True))


def _delta(chunk: str) -> str:
    """Extract the text delta from an upstream SSE line, tolerating both raw text and `data:` JSON."""
    line = chunk[5:].strip() if chunk.startswith("data:") else chunk.strip()
    if not line or line == "[DONE]":
        return ""
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except ValueError:
            return line
        return (obj.get("token") or obj.get("text") or obj.get("content")
                or (obj.get("choices") or [{}])[0].get("delta", {}).get("content") or "")
    return line


def _sse_error(status: int, body: str) -> str:
    code = "gpu_busy" if status in (409, 503, 507) else "error"
    return ("event: error\ndata: " + json.dumps(
        {"error": {"code": code, "message": body[:200]}}) + "\n\n")


# -- embeddings ------------------------------------------------------------------------------------

class EmbeddingsRequest(BaseModel):
    model: str = ""
    input: list[str] | str = Field(default_factory=list)


@router.post("/embeddings")
async def embeddings(body: EmbeddingsRequest, request: Request,
                     tenant: dict = Depends(require_tenant)):
    """OpenAI-compatible embeddings.

    The embed child is CPU-only and holds no GPU lease (Principle II), so this settles ~0 GPU-seconds
    — but it is still reserved and settled, because "which tenant asked for what" is the attribution
    the ledger exists to answer, and a modality that skipped it would be a hole in the audit.
    """
    texts = [body.input] if isinstance(body.input, str) else list(body.input)
    if not texts:
        raise refuse("forbidden", "input must not be empty")
    meter = _Meter(request, tenant, "embeddings", est=1.0)
    with BROKER_LATENCY.labels(modality="embeddings").time():
        with meter:
            r = await _post(f"{EMBED_URL}/embed", {"texts": texts}, timeout=120.0)
            if r.status_code != 200:
                meter.abandon()
                BROKER_REQUESTS.labels(modality="embeddings", status="refused").inc()
                _map_agent_refusal(r.status_code, r.text, model=body.model)
            vectors = r.json()
            if isinstance(vectors, dict):
                vectors = vectors.get("vectors") or []
            meter.finish(meter.elapsed())
            BROKER_REQUESTS.labels(modality="embeddings", status="ok").inc()
            return Response(
                content=json.dumps({
                    "object": "list", "model": body.model or "embed",
                    "data": [{"object": "embedding", "index": i, "embedding": v}
                             for i, v in enumerate(vectors)],
                    "usage": {"prompt_tokens": 0, "total_tokens": 0,
                              "gpu_seconds": meter.gpu_seconds}}),
                media_type="application/json", headers=meter.headers())


# -- audio transcriptions ----------------------------------------------------------------------------

@router.post("/audio/transcriptions")
async def transcriptions(request: Request, tenant: dict = Depends(require_tenant)):
    """OpenAI-compatible multipart transcription (`file`, `model`) -> `{text}`.

    Multipart rather than JSON because that is what the OpenAI clients send; a JSON-only endpoint
    would mean every tenant writing a custom uploader, which is exactly the friction this surface
    exists to remove. A base64 JSON body is also accepted for `curl` ergonomics.
    """
    import base64

    content_type = request.headers.get("content-type", "")
    model, language = "", "auto"
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise refuse("forbidden", "multipart body must carry a `file` part")
        audio = await upload.read() if hasattr(upload, "read") else bytes(upload)
        filename = getattr(upload, "filename", "audio.wav")
        model = str(form.get("model") or "")
        language = str(form.get("language") or "auto")
        audio_b64 = base64.b64encode(audio).decode("ascii")
    else:
        body = await request.json()
        audio_b64 = body.get("audio_b64") or body.get("file") or ""
        filename = body.get("filename", "audio.wav")
        model = body.get("model", "")
        language = body.get("language", "auto")
        if not audio_b64:
            raise refuse("forbidden", "send multipart `file`, or JSON with `audio_b64`")

    meter = _Meter(request, tenant, "asr")
    with BROKER_LATENCY.labels(modality="asr").time():
        with meter:
            r = await _post(f"{ASR_URL}/transcribe",
                            {"audio_b64": audio_b64, "filename": filename, "language": language})
            if r.status_code != 200:
                meter.abandon()
                BROKER_REQUESTS.labels(modality="asr", status="refused").inc()
                _map_agent_refusal(r.status_code, r.text, model=model)
            payload = r.json()
            meter.finish(meter.elapsed())
            BROKER_REQUESTS.labels(modality="asr", status="ok").inc()
            return Response(content=json.dumps({"text": payload.get("text", "")}),
                            media_type="application/json", headers=meter.headers())


# -- computer vision (task-typed) ----------------------------------------------------------------------

class VisionRequest(BaseModel):
    model: str = ""
    image: str = Field("", description="base64 image bytes, or a LAN-reachable URL")


@router.post("/vision/{task}")
async def vision(task: str, body: VisionRequest, request: Request,
                 tenant: dict = Depends(require_tenant)):
    """`classify` -> `{labels:[{label,score}]}`; `detect` -> `{objects:[{label,score,box}]}`."""
    if task not in ("classify", "detect"):
        raise refuse("not_found", f"unknown vision task {task!r} (expected classify|detect)")
    if not body.image:
        raise refuse("forbidden", "image must be base64 bytes or a LAN-reachable URL")

    meter = _Meter(request, tenant, f"vision.{task}")
    with BROKER_LATENCY.labels(modality="vision").time():
        with meter:
            r = await _post(f"{BENTO_URL}/{task}", {"image_b64": body.image})
            if r.status_code != 200:
                meter.abandon()
                BROKER_REQUESTS.labels(modality="vision", status="refused").inc()
                _map_agent_refusal(r.status_code, r.text, model=body.model)
            payload = r.json()
            meter.finish(meter.elapsed())
            BROKER_REQUESTS.labels(modality="vision", status="ok").inc()
            if task == "classify":
                result = {"labels": payload.get("labels") or payload.get("predictions") or []}
            else:
                result = {"objects": payload.get("objects") or payload.get("detections") or []}
            return Response(content=json.dumps(result), media_type="application/json",
                            headers=meter.headers())


# -- tenant self-service usage ---------------------------------------------------------------------------

@router.get("/usage")
def my_usage(tenant: dict = Depends(require_tenant)):
    """A tenant's OWN usage. Scoped to the authenticated tenant by construction — there is no path
    parameter to confuse, which is the simplest possible answer to the cross-tenant question (T633).
    """
    state = quota_state(tenant["id"])
    return {"tenant": tenant["name"], "window": state.get("window"),
            "window_start": state.get("window_start"),
            "budget_gpu_seconds": state.get("budget_gpu_seconds"),
            "consumed_gpu_seconds": state.get("consumed_gpu_seconds"),
            "remaining_gpu_seconds": state.get("remaining_gpu_seconds")}
