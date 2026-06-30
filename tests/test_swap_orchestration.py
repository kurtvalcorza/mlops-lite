"""017 US1/US2 — gateway swap orchestration (T329, FR-154/155/157/158, SC-100/101/102).

Offline + GPU-free: load `gateway/app/swap.py` in isolation and drive `preempt_if_needed` with injected
seams (`state_fn`, `http_post`, `sleep`) — no live daemons, no lease file. Asserts the swap resolves the
holder → sends `unload-now` to the right supervisor → waits for the lease to free; that a training holder
is refused (never evicted); and that the default (no-preempt) path never enters here.
"""
import asyncio
import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_swap():
    spec = importlib.util.spec_from_file_location(
        "swap_under_test", os.path.join(REPO, "gateway", "app", "swap.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load_swap()


class StateSeq:
    """An async holder-state seam returning a scripted sequence; the last entry repeats. Records calls."""

    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    async def __call__(self):
        i = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return self.states[i]


class PostRecorder:
    """An async http_post seam: records (url, body) and returns a scripted (status, body)."""

    def __init__(self, status=200, body=None):
        self.status, self.body = status, (body or {"status": "unloaded", "drained": True})
        self.calls = []

    async def __call__(self, url, json_body):
        self.calls.append((url, json_body))
        return self.status, self.body


async def _nosleep(_):  # never actually waits in tests
    return None


def run(coro):
    return asyncio.run(coro)


def test_serving_holder_is_evicted_then_free():
    # LLM resident, target = vision → unload-now to the LLM supervisor, then the lease frees.
    state = StateSeq({"holder": "llm", "resident": True}, {"holder": None, "resident": False})
    post = PostRecorder()
    res = run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep))
    assert res["swapped"] is True and res["evicted"] == "llm"
    assert len(post.calls) == 1
    url, body = post.calls[0]
    assert url.endswith("/unload-now") and "8090" in url   # the LLM supervisor URL
    assert body["drain_timeout_s"] == m.SWAP_DRAIN_TIMEOUT_S


def test_no_holder_no_swap():
    state = StateSeq({"holder": None, "resident": False})
    post = PostRecorder()
    res = run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep))
    assert res["swapped"] is False and post.calls == []  # nothing to evict → no unload-now


def test_holder_is_already_target_no_swap():
    state = StateSeq({"holder": "vision", "resident": True})
    post = PostRecorder()
    res = run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep))
    assert res["swapped"] is False and post.calls == []  # already the target → just serve


def test_training_holder_is_refused_never_evicted():
    # FR-155 / SC-102: a training holder is refused; NO unload-now is sent.
    state = StateSeq({"holder": "training", "resident": True})
    post = PostRecorder()
    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep))
    except m.PreemptRefused as e:
        assert "not preemptable" in str(e)
    else:
        raise AssertionError("expected PreemptRefused for a training holder")
    assert post.calls == []  # the training job was never told to unload


def test_unknown_serving_holder_refused_not_guessed():
    state = StateSeq({"holder": "mystery", "resident": True})
    post = PostRecorder()
    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep))
    except m.PreemptRefused:
        pass
    else:
        raise AssertionError("expected PreemptRefused for an unmapped holder")
    assert post.calls == []  # never unload a holder we can't identify


def test_unload_now_failure_is_a_swap_error():
    state = StateSeq({"holder": "llm", "resident": True})
    post = PostRecorder(status=500, body={"error": "boom"})
    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep))
    except m.SwapError:
        pass
    else:
        raise AssertionError("expected SwapError when unload-now returns non-200")


def test_unload_now_busy_body_is_a_swap_error():
    # the holder returns 200 but couldn't evict (vision's in-process model refuses a hard-cut to keep
    # one-model-in-VRAM) → status "busy" must be a SwapError, never a "proceed onto an occupied GPU".
    state = StateSeq({"holder": "vision", "resident": True})
    post = PostRecorder(status=200, body={"status": "busy", "detail": "did not drain"})
    try:
        run(m.preempt_if_needed("llm", state_fn=state, http_post=post, sleep=_nosleep))
    except m.SwapError as e:
        assert "did not unload" in str(e)
    else:
        raise AssertionError("expected SwapError when unload-now returns a non-unloaded status")


def test_lease_never_frees_is_a_swap_error():
    # holder stays resident forever after unload-now → wait-for-free times out.
    state = StateSeq({"holder": "llm", "resident": True})  # always returns the holder
    post = PostRecorder()
    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep,
                                free_wait_s=1.0))
    except m.SwapError as e:
        assert "did not free" in str(e)
    else:
        raise AssertionError("expected SwapError when the lease never frees")


def test_asr_holder_evicted_via_asr_url():
    state = StateSeq({"holder": "asr", "resident": True}, {"holder": None, "resident": False})
    post = PostRecorder()
    run(m.preempt_if_needed("llm", state_fn=state, http_post=post, sleep=_nosleep))
    assert "8095" in post.calls[0][0]  # the ASR supervisor URL


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
