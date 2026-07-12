"""Streaming router (003 US1, T063): SSE endpoints the operator UI consumes.

`POST /infer/stream` proxies the serving supervisor's token stream as Server-Sent Events, holding
the same GPU lock as the non-streaming path so at most one inference is in flight (Principle II —
the gateway is the authority). `/infer` (REST) stays intact for non-UI clients and the 001 smoke.

`GET /runs/{run_id}/events` bridges the trainer's poll API (`GET /training/{id}`) to SSE — the
gateway polls and re-emits status/metrics until the run reaches a terminal state.
"""
import asyncio
import json
import os
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import platform_health, quality, registry, serving, tracing
from ..settings import agent_headers
from .runs import TRAINER_URL

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# The supervisor's terminal SSE frame — a delivered `done` proves a complete stream (006 trace status).
_DONE_MARKER = b'"event": "done"'


class StreamRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.7
    preempt: bool = False  # 017: opt-in swap — evict a resident *serving* model first (default 008 refuse)


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


@router.post("/infer/stream")
async def infer_stream(req: StreamRequest):
    """Stream inference tokens as SSE (FR-026/FR-027). Auth is enforced by the router dependency."""
    route_start_ns = time.time_ns()
    if not await serving.health():
        # 006/FR-050: trace the pre-generation failure too — parity with REST /infer's 503 branch, so a
        # failed stream during a serving outage is still observable (gen() never runs in this case).
        # 022 (FR-262): the agent is unreachable here, so the served identity is genuinely `unknown` —
        # never the fixed SERVING_MODEL config guess (the divergence 022 removes).
        tracing.emit(
            name="infer_stream",
            inputs={"prompt": req.prompt},
            attributes={"max_tokens": req.max_tokens, "temperature": req.temperature,
                        "model": "unknown", "status": "unavailable", "token_frames": 0},
            start_ns=route_start_ns, end_ns=time.time_ns(), status="ERROR",
        )
        raise HTTPException(status_code=503, detail="serving backend (supervisor) not reachable")

    # 022 US2 (FR-261/262): resolve the AGENT-reported served identity ONCE, up front — the agent is
    # reachable (health just passed), and a request holds the GPU lease for its WHOLE generation, so
    # this identity cannot drift mid-stream. Both the trace and the prediction log below attribute to
    # it, never the static SERVING_MODEL. Resolved OUTSIDE the generator so no await runs on the
    # teardown/cancel path (the reason the prior version deferred it to a detached task). Version
    # falls back to the served name's own @serving alias when the agent reports none (legacy agent).
    _ident = await serving.llm_identity()
    served_model, served_version = _ident["serving_model"], _ident["serving_version"]
    if served_model != "unknown" and served_version is None:
        try:
            _s = await run_in_threadpool(registry.get_serving, served_model)
            served_version = _s["version"] if _s else None
        except Exception:
            served_version = None

    # T363: preempt is forwarded to the AGENT (as `?preempt=true` below), which orchestrates it
    # under its single admission lock — the gateway no longer brokers it. A refusal (a `kind="job"`
    # holder, or a failed evict) returns the agent's 409 *before* the SSE opens, surfaced as the
    # stream's pre-generation error frame (the status != 200 branch); the non-stream /infer keeps a
    # clean 409 body via ServingBusyError.
    _preempt_q = "?preempt=true" if req.preempt else ""
    infer_stream_url = f"{serving.SERVING_URL}/infer/stream" + _preempt_q

    async def gen():
        # 006/FR-050: trace timing captured OUTSIDE the GPU lock (export never coincides with the
        # mutex) and emitted fire-and-forget in the finally. The SSE bytes are an untouched passthrough.
        start_ns = time.time_ns()
        frames = 0
        saw_done = False
        done_tail = b""  # rolling overlap so a `done` frame split across transport chunks is still seen
        # Pessimistic default: an unexpected mid-stream failure leaves the trace errored — only a stream
        # that delivered the terminal `done` frame flips it to OK below (parity with REST /infer; 006
        # Codex review).
        trace_status, outcome, error_detail = "ERROR", "error", None
        try:
            # Hold the GPU lock for the whole generation — serializes with the non-streaming path.
            async with serving._gpu_lock:
                try:
                    async with httpx.AsyncClient(headers=agent_headers(), timeout=300) as client:
                        async with client.stream(
                            "POST", infer_stream_url,
                            json={"prompt": req.prompt, "max_tokens": req.max_tokens,
                                  "temperature": req.temperature},
                        ) as r:
                            if r.status_code != 200:
                                body = (await r.aread()).decode("utf-8", "ignore")[:200]
                                trace_status, outcome, error_detail = "ERROR", "error", body
                                yield _sse({"event": "error", "detail": body})
                                return
                            # Pass the supervisor's SSE bytes straight through (already framed).
                            # Count `data:` frames as an approximate token count — no parsing, bytes
                            # stay byte-identical (FR-050).
                            async for chunk in r.aiter_raw():
                                frames += chunk.count(b"data:")
                                if not saw_done:
                                    # aiter_raw yields transport chunks, not SSE frames — the terminal
                                    # `done` frame can split across chunks, so scan a rolling window
                                    # (prev tail + chunk), not the chunk alone (006 Codex review).
                                    window = done_tail + chunk
                                    if _DONE_MARKER in window:
                                        saw_done = True
                                    else:
                                        done_tail = window[-len(_DONE_MARKER):]
                                yield chunk
                            # aiter_raw ending != success: the supervisor can close the response
                            # mid-stream on a backend failure with no error frame, so only a delivered
                            # terminal `done` frame proves a complete stream (006 Codex review).
                            if saw_done:
                                trace_status, outcome = "OK", "completed"
                            else:
                                outcome, error_detail = "truncated", "stream closed before done frame"
                except httpx.HTTPError as e:
                    trace_status, outcome, error_detail = "ERROR", "error", f"serving unreachable: {e}"
                    yield _sse({"event": "error", "detail": f"serving unreachable: {e}"})
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected mid-stream — record an aborted generation, not a false success.
            trace_status, outcome, error_detail = "ERROR", "cancelled", "client disconnected mid-stream"
            raise
        finally:
            attrs = {
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
                "model": served_model,  # 022: the agent-reported served identity, not the config
                "status": outcome,
                "token_frames": frames,
            }
            if error_detail:
                attrs["error"] = error_detail
            tracing.emit(
                name="infer_stream",
                inputs={"prompt": req.prompt},
                outputs=None,
                attributes=attrs,
                start_ns=start_ns,
                end_ns=time.time_ns(),
                status=trace_status,
            )
            # 013/FR-119: log the served prediction off the request path (fire-and-forget, fail-open).
            # The streamed tokens aren't buffered (the SSE bytes stay byte-identical), so the output is
            # left uncaptured — the prediction id + prompt + version are still logged for later labeling.
            # 022 US2 (FR-261): attributed to the AGENT-REPORTED identity resolved up front (above),
            # not the fixed SERVING_MODEL — the divergence that corrupted a served fine-tune's quality
            # window. quality.log_prediction is itself fire-and-forget (spawns its own daemon thread +
            # returns synchronously), so with the resolve already done it never delays teardown — the
            # detached-task wrapper the pre-resolve version needed is no longer required.
            if outcome == "completed":
                quality.log_prediction(served_model, served_version, "text-generation",
                                       req.prompt, None)
                # 016 (FR-146): do NOT capture streamed prompts. The streamed output isn't logged
                # (prediction=None), so join_window excludes these as champion-unscorable — but a
                # capture would still consume the per-modality ring-buffer cap, evicting replayable
                # REST inputs before their labels arrive and starving shadow-replay. Capture only
                # where the prediction is logged (the /infer path).

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


async def _state_snapshot(client: httpx.AsyncClient) -> dict:
    """One platform snapshot: daemon reachability + serving-resident + GPU-free (best-effort)."""
    health = await platform_health.aggregate()
    serving_detail, gpu_free = None, None
    try:
        r = await client.get(f"{serving.SERVING_URL}/health", timeout=3)
        if r.status_code == 200:
            d = r.json()
            serving_detail = {
                "resident": d.get("resident"),
                "est_vram_gb": d.get("est_vram_gb"),
                "fits": d.get("fits"),
                "vram_budget_gb": d.get("vram_budget_gb"),
            }
    except httpx.HTTPError:
        pass
    try:
        r = await client.get(f"{TRAINER_URL}/health", timeout=3)
        if r.status_code == 200:
            gpu_free = r.json().get("gpu_free_mib")
    except httpx.HTTPError:
        pass
    return {
        "event": "state",
        "all_healthy": health["all_healthy"],
        "daemons": health["daemons"],
        "serving": serving_detail,
        "gpu_free": gpu_free,
    }


@router.get("/platform/events")
async def platform_events():
    """Live platform state as SSE (003 US2, FR-029): periodic daemon/GPU/serving snapshot.

    Re-emits the same data as `GET /platform/health` (+ GPU/resident detail) on an interval so the
    Health tab updates without polling. Read-only — surfaces state, never touches the VRAM mutex.
    """

    async def gen():
        interval = float(os.getenv("STATE_POLL_INTERVAL", "2"))
        last = None
        async with httpx.AsyncClient(headers=agent_headers(), timeout=5) as client:
            while True:
                try:
                    snap = await _state_snapshot(client)
                except Exception as e:  # never break the stream on a transient probe error
                    yield _sse({"event": "error", "detail": f"state probe failed: {e}"})
                    await asyncio.sleep(interval)
                    continue
                payload = json.dumps(snap, sort_keys=True)
                if payload != last:        # only push on change
                    last = payload
                    yield _sse(snap)
                await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    """Bridge the trainer's poll API to SSE (FR-026): emit status until a terminal state."""

    async def gen():
        terminal = {"completed", "failed"}
        last = None
        async with httpx.AsyncClient(headers=agent_headers(), timeout=10) as client:
            while True:
                try:
                    r = await client.get(f"{TRAINER_URL}/train/{run_id}")
                    rec = r.json() if r.status_code == 200 else {"status": "unknown"}
                except httpx.HTTPError as e:
                    yield _sse({"event": "error", "detail": f"trainer unreachable: {e}"})
                    return
                snap = json.dumps(rec, sort_keys=True)
                if snap != last:           # only push on change
                    last = snap
                    yield _sse({"event": "run", **rec})
                if rec.get("status") in terminal:
                    return
                await asyncio.sleep(float(os.getenv("RUN_POLL_INTERVAL", "2")))

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
