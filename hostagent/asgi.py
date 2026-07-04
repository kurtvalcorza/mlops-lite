"""ASGI transport for the agent (020 US3, T413 — FR-205/206; contracts/children-api.md §runtime).

The `AGENT_RUNTIME=uvicorn` half of the dual-runtime switch: a single raw ASGI app (no web
framework — the route table stays the transport-neutral `handle_get`/`handle_post` in
`hostagent/main.py`, shared verbatim with the stdlib server). Preserved regardless of runtime:
`AGENT_BIND` posture, `X-Agent-Control` enforcement, the 008–017 error vocabulary, `/metrics`
output shape, SSE stream framing, and the legacy byte-compat aliases.

Streaming: `stream_frames` holds the engine's RLock across the whole generation, and an RLock is
thread-affine — so each stream gets ONE dedicated pump thread that drives the generator end-to-end
(acquire, yield, close/release all on that thread). Frames hand off to the event loop over an
`asyncio.Queue` via `call_soon_threadsafe`, bounded by a semaphore the consumer releases — so the
async side `await`s natively (fully cancellable) and NO shared executor thread is parked per
stream — parking per-stream `q.get` calls on the default executor could exhaust the pool that
also dispatches `handle_get`/`handle_post` under enough hung/disconnected streams. The first
frame is pulled before headers are sent, so a pre-stream failure still answers a JSON error with
the mapped status (same as the stdlib transport). A client disconnect stops the pump between
frames; the pump's `finally` closes the generator on its own thread, which releases the lock —
the next request is clean (FR-205's disconnect scenario).

Imported lazily by `main()` only when `AGENT_RUNTIME=uvicorn` is selected — the stdlib default
keeps the agent pip-dep-free.
"""
import asyncio
import json
import threading

from hostagent import main as agent_main

_QUEUE_FRAMES = 8          # small bound: backpressure a slow client instead of buffering unbounded
_ACQUIRE_POLL_S = 0.25     # how often a backpressured pump re-checks the stop flag


class _StreamPump:
    """Drives a sync frame generator on one dedicated thread (RLock affinity), feeding an
    asyncio.Queue of ("frame", bytes) | ("end", None) | ("error", exc) items over
    `call_soon_threadsafe`. Backpressure = a semaphore the consumer releases per item, so the
    async side never parks an executor thread just to wait for a frame."""

    def __init__(self, gen, loop):
        self._gen = gen
        self._loop = loop
        self.q = asyncio.Queue()
        self._slots = threading.Semaphore(_QUEUE_FRAMES)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _emit(self, item) -> bool:
        while not self._stop.is_set():
            if not self._slots.acquire(timeout=_ACQUIRE_POLL_S):
                continue  # queue full — re-check the stop flag, then wait again
            if self._stop.is_set():
                return False  # raced a stop — don't enqueue into a finished consumer
            try:
                self._loop.call_soon_threadsafe(self.q.put_nowait, item)
                return True
            except RuntimeError:
                return False  # event loop closed (server shutdown mid-stream)
        return False

    def _run(self):
        try:
            for frame in self._gen:
                if not self._emit(("frame", frame)):
                    return  # stopped (client gone) — finally closes the generator
            self._emit(("end", None))
        except Exception as e:  # noqa: BLE001 — surfaced to the transport for status mapping
            self._emit(("error", e))
        finally:
            # Close ON THIS THREAD: the generator acquired the engine RLock here, so GeneratorExit
            # (→ lock release + touch) must run here too.
            self._gen.close()

    def item_consumed(self):
        self._slots.release()

    def stop(self):
        self._stop.set()


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
    """First-frame-before-headers, then relay; stop on disconnect (watched via the ASGI
    `http.disconnect` message, not just inferred from a failing send)."""
    pump = _StreamPump(frames, asyncio.get_running_loop())
    watcher = asyncio.ensure_future(_watch_disconnect(receive))
    try:
        kind, item = await pump.q.get()
        pump.item_consumed()
        if kind == "error":
            status, payload = agent_main.error_response(item)
            return await _send_json(send, status, payload)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"),
                                (b"cache-control", b"no-cache")]})
        if kind == "frame":
            await send({"type": "http.response.body", "body": item, "more_body": True})
            while True:
                get_task = asyncio.ensure_future(pump.q.get())
                done, _ = await asyncio.wait({get_task, watcher},
                                             return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    get_task.cancel()  # native cancel — no executor thread left parked
                    break  # client disconnected mid-stream — pump stops, lock releases there
                kind, item = get_task.result()
                pump.item_consumed()
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

        if method == "GET":
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
        # The stdlib BaseHTTPRequestHandler answers any verb without a do_<VERB> with a 501 —
        # mirror that (the agent's contract is GET/POST only; nothing pins the other verbs).
        return await _send_json(send, 501, {"error": f"unsupported method {method}"})

    return app
