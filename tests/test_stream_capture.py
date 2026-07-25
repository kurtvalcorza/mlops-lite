"""025 US4 (T611) — a streamed prediction is labelable, and the stream stays a passthrough (FR-356).

A completed `/infer/stream` was already logged for quality, but `quality.log_prediction` mints the id
internally — so the caller never learned it, the label endpoint (which takes a **caller-supplied** id,
with no prediction-list endpoint to look one up) could not be used, and SC-180 was unreachable for
streaming alone. The route now emits ONE leading metadata frame carrying the id and logs under that same
id.

Two things must hold together, which is why a seam-only test is not enough (Codex round-4): the id must
actually reach the WIRE, **and** the supervisor's own frames must stay byte-identical. Both are asserted
here against the real route, with the agent/serving/quality seams faked (no live stack, no GPU).
"""
import asyncio
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.routers import stream as st  # noqa: E402

# The supervisor's own SSE bytes — what must survive untouched, including a split `done` frame.
UPSTREAM = [b'data: {"event": "start"}\n\n',
            b'data: {"event": "token", "text": "hel"}\n\n',
            b'data: {"event": "token", "text": "lo"}\n\n',
            b'data: {"event": "do',                       # deliberately split across chunks
            b'ne"}\n\n']


class FakeStreamResp:
    def __init__(self, chunks, status=200):
        self._chunks, self.status_code = chunks, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b"upstream error body"


class FakeAsyncClient:
    def __init__(self, chunks, status=200):
        self._chunks, self._status = chunks, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None):
        return FakeStreamResp(self._chunks, self._status)


def _wire(monkeypatch, chunks=None, status=200):
    """Fake the agent/serving/tracing/quality seams and collect what got logged."""
    logged = []
    monkeypatch.setattr(st.httpx, "AsyncClient",
                        lambda **kw: FakeAsyncClient(UPSTREAM if chunks is None else chunks, status))

    async def _health():
        return True

    async def _ident():
        return {"serving_model": "qwen-ft", "serving_version": "5"}

    monkeypatch.setattr(st.serving, "health", _health)
    monkeypatch.setattr(st.serving, "llm_identity", _ident)
    monkeypatch.setattr(st.serving, "_gpu_lock", asyncio.Lock())
    monkeypatch.setattr(st.tracing, "emit", lambda **kw: None)
    monkeypatch.setattr(st.quality, "log_prediction",
                        lambda name, version, modality, input_ref, prediction, prediction_id=None:
                        logged.append(dict(name=name, version=version, modality=modality,
                                           prediction=prediction, pid=prediction_id)) or prediction_id)
    return logged


def _collect(chunks_out):
    """Drive the route and return (frames_as_bytes, parsed_metadata_or_None)."""
    async def _go():
        resp = await st.infer_stream(st.StreamRequest(prompt="hi", max_tokens=8))
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
        return out

    got = asyncio.run(_go())
    chunks_out.extend(got)
    meta = None
    if got and b'"metadata"' in got[0]:
        meta = json.loads(got[0].decode().split("data: ", 1)[1])
    return got, meta


# --- the id reaches the wire -----------------------------------------------------------------------

def test_metadata_frame_carries_the_prediction_id_first(monkeypatch):
    _wire(monkeypatch)
    frames, meta = _collect([])
    assert meta is not None, f"first frame must be the metadata event; got {frames[:1]}"
    assert meta["event"] == "metadata" and meta["prediction_id"]
    assert len(meta["prediction_id"]) == 32                 # a uuid4 hex, minted per request
    assert meta["model"] == "qwen-ft" and meta["version"] == "5"


def test_logged_row_uses_the_same_id_the_client_received(monkeypatch):
    logged = _wire(monkeypatch)
    _, meta = _collect([])
    assert [r["pid"] for r in logged] == [meta["prediction_id"]]   # the label will land on THIS row
    assert logged[0]["name"] == "qwen-ft" and logged[0]["version"] == "5"
    assert logged[0]["modality"] == "text-generation"
    assert logged[0]["prediction"] is None      # streamed → output uncaptured (store marks streamed)


def test_each_request_gets_a_distinct_id(monkeypatch):
    _wire(monkeypatch)
    _, m1 = _collect([])
    _, m2 = _collect([])
    assert m1["prediction_id"] != m2["prediction_id"]


# --- the supervisor's frames stay byte-identical ---------------------------------------------------

def test_upstream_frames_are_passed_through_byte_for_byte(monkeypatch):
    _wire(monkeypatch)
    frames, _ = _collect([])
    # frame 0 is ours; everything after it is the supervisor's bytes, in order, unmodified.
    assert frames[1:] == UPSTREAM
    # and the concatenated upstream payload is untouched (incl. the split `done` frame reassembling)
    assert b"".join(frames[1:]) == b"".join(UPSTREAM)
    assert b'"event": "done"' in b"".join(frames[1:])


def test_exactly_one_frame_is_added(monkeypatch):
    _wire(monkeypatch)
    frames, _ = _collect([])
    assert len(frames) == len(UPSTREAM) + 1


# --- failure paths: no false labelability ----------------------------------------------------------

def test_a_truncated_stream_logs_nothing_so_the_id_is_not_labelable(monkeypatch):
    """The id is handed over early, but the row is only written when the stream COMPLETES (the
    pre-existing rule). An aborted stream must not leave a labelable-looking id with no row."""
    logged = _wire(monkeypatch, chunks=[b'data: {"event": "start"}\n\n'])   # never reaches `done`
    frames, meta = _collect([])
    assert meta["prediction_id"]            # the client still got one
    assert logged == []                     # …but nothing was logged under it


def test_an_upstream_error_still_emits_metadata_then_the_error_frame(monkeypatch):
    logged = _wire(monkeypatch, chunks=[], status=500)
    frames, meta = _collect([])
    assert meta["event"] == "metadata"                       # metadata precedes the failure
    assert b'"event": "error"' in frames[1]
    assert logged == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
