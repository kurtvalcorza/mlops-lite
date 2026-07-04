"""ASGI transport for the agent (020 US3, T413 — FR-205/206; contracts/children-api.md §runtime).

The `AGENT_RUNTIME=uvicorn` half of the dual-runtime switch: a single raw ASGI app (no web
framework — the route table stays the transport-neutral `handle_get`/`handle_post` in
`hostagent/main.py`, shared verbatim with the stdlib server). Preserved regardless of runtime:
`AGENT_BIND` posture, `X-Agent-Control` enforcement, the 008–017 error vocabulary, `/metrics`
output shape, SSE stream framing, and the legacy byte-compat aliases.

Streaming: `stream_frames` holds the engine's RLock across the whole generation, and an RLock is
thread-affine — so each stream gets ONE dedicated pump thread that drives the generator end-to-end
(acquire, yield, close/release all on that thread), handing frames to the event loop over a small
bounded queue. The first frame is pulled before headers are sent, so a pre-stream failure still
answers a JSON error with the mapped status (same as the stdlib transport). A client disconnect
stops the pump between frames; the pump's `finally` closes the generator on its own thread, which
releases the lock — the next request is clean (FR-205's disconnect scenario).

Imported lazily by `main()` only when `AGENT_RUNTIME=uvicorn` is selected — the stdlib default
keeps the agent pip-dep-free.
"""
import asyncio
import json
import queue
import threading

from hostagent import main as agent_main

_QUEUE_FRAMES = 8          # small bound: backpressure a slow client instead of buffering unbounded
_PUT_POLL_S = 0.25         # how often a blocked pump re-checks the stop flag


class _StreamPump:
    """Drives a sync frame generator on one dedicated thread (RLock affinity), feeding a bounded
    queue of ("frame", bytes) | ("end", None) | ("error", exc) items."""

    def __init__(self, gen):
        self._gen = gen
        self.q = queue.Queue(maxsize=_QUEUE_FRAMES)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _put(self, item) -> bool:
        while not self._stop.is_set():
            try:
                self.q.put(item, timeout=_PUT_POLL_S)
                return True
            except queue.Full:
                continue
        return False

    def _run(self):
        try:
            for frame in self._gen:
                if not self._put(("frame", frame)):
                    return  # stopped (client gone) — finally closes the generator
            self._put(("end", None))
        except Exception as e:  # noqa: BLE001 — surfaced to the transport for status mapping
            self._put(("error", e))
        finally:
            # Close ON THIS THREAD: the generator acquired the engine RLock here, so GeneratorExit
            # (→ lock release + touch) must run here too.
            self._gen.close()

    def stop(self):
        self._stop.set()
        # Drain one slot so a pump blocked in q.put wakes promptly.
        try:
            self.q.get_nowait()
        except queue.Empty:
            pass


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return b""
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body"):
            return b"".join(chunks)


async def _send_json(send, status: int, payload, content_type: str = "application/json"):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type.encode()),
                            (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


async def _watch_disconnect(receive):
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return


async def _serve_stream(send, receive, frames):
    """First-frame-before-headers, then relay; stop on disconnect (watched, not just inferred
    from a failing send)."""
    pump = _StreamPump(frames)
    loop = asyncio.get_running_loop()
    watcher = asyncio.ensure_future(_watch_disconnect(receive))
    try:
        kind, item = await loop.run_in_executor(None, pump.q.get)
        if kind == "error":
            status, payload = agent_main.error_response(item)
            return await _send_json(send, status, payload)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"),
                                (b"cache-control", b"no-cache")]})
        if kind == "frame":
            await send({"type": "http.response.body", "body": item, "more_body": True})
            while True:
                get_task = asyncio.ensure_future(loop.run_in_executor(None, pump.q.get))
                done, _ = await asyncio.wait({get_task, watcher},
                                             return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    get_task.cancel()
                    break  # client disconnected mid-stream — stop pumping, lock releases in pump
                kind, item = get_task.result()
                if kind != "frame":
                    break  # "end" — or a mid-stream error, which the stdlib path also truncates
                try:
                    await send({"type": "http.response.body", "body": item, "more_body": True})
                except Exception:
                    break  # transport-level disconnect
        await send({"type": "http.response.body", "body": b""})
    except Exception:
        pass  # response already started — nothing coherent left to send
    finally:
        pump.stop()
        if not watcher.done():
            watcher.cancel()


def build_asgi_app(admission, journal, manager, jobs):
    """The raw ASGI application — same components, same routers as `make_handler`."""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":  # served with lifespan="off", but be tolerant
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope["path"]
        query = scope.get("query_string", b"").decode()
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope["method"]

        if method in ("GET", "HEAD"):
            code, payload, ctype = await asyncio.to_thread(
                agent_main.handle_get, path, query,
                admission=admission, journal=journal, manager=manager, jobs=jobs)
            return await _send_json(send, code, payload, content_type=ctype)
        if method == "POST":
            raw = await _read_body(receive)
            result = await asyncio.to_thread(
                agent_main.handle_post, path, query, headers.get("content-type", ""),
                headers.get("x-agent-control", ""), raw,
                admission=admission, journal=journal, manager=manager, jobs=jobs)
            if result[0] == "json":
                _, code, payload = result
                return await _send_json(send, code, payload)
            return await _serve_stream(send, receive, result[1])
        return await _send_json(send, 405, {"error": "method not allowed"})

    return app
