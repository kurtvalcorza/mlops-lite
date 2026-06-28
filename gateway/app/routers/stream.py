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

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import platform_health, serving
from .runs import TRAINER_URL

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class StreamRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.7


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


@router.post("/infer/stream")
async def infer_stream(req: StreamRequest):
    """Stream inference tokens as SSE (FR-026/FR-027). Auth is enforced by the router dependency."""
    if not await serving.health():
        raise HTTPException(status_code=503, detail="serving backend (supervisor) not reachable")

    async def gen():
        # Hold the GPU lock for the whole generation — serializes with the non-streaming path.
        async with serving._gpu_lock:
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream(
                        "POST", f"{serving.SERVING_URL}/infer/stream",
                        json={"prompt": req.prompt, "max_tokens": req.max_tokens,
                              "temperature": req.temperature},
                    ) as r:
                        if r.status_code != 200:
                            body = (await r.aread()).decode("utf-8", "ignore")[:200]
                            yield _sse({"event": "error", "detail": body})
                            return
                        # Pass the supervisor's SSE bytes straight through (already framed).
                        async for chunk in r.aiter_raw():
                            yield chunk
            except httpx.HTTPError as e:
                yield _sse({"event": "error", "detail": f"serving unreachable: {e}"})

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
        async with httpx.AsyncClient(timeout=5) as client:
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
        async with httpx.AsyncClient(timeout=10) as client:
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
