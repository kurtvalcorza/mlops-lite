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


async def _up(_label):    # swap target daemon reachable (health probe ok)
    return True


async def _nobatch():     # no GPU batch active in the trainer
    return False


def _preempt(target, **kw):
    """Existing tests drive the holder-resolution logic; default the new guards to 'target up, no batch'
    so they don't fall through to the live-HTTP defaults. The guard-specific tests pass these explicitly."""
    kw.setdefault("target_probe_fn", _up)
    kw.setdefault("batch_active_fn", _nobatch)
    return m.preempt_if_needed(target, **kw)


def test_serving_holder_is_evicted_then_free():
    # LLM resident, target = vision → unload-now to the LLM supervisor, then the lease frees.
    state = StateSeq({"holder": "llm", "resident": True}, {"holder": None, "resident": False})
    post = PostRecorder()
    res = run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep))
    assert res["swapped"] is True and res["evicted"] == "llm"
    assert len(post.calls) == 1
    url, body = post.calls[0]
    assert url.endswith("/unload-now") and "8090" in url   # the LLM supervisor URL
    assert body["drain_timeout_s"] == m.SWAP_DRAIN_TIMEOUT_S


def test_no_holder_no_swap():
    state = StateSeq({"holder": None, "resident": False})
    post = PostRecorder()
    res = run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep))
    assert res["swapped"] is False and post.calls == []  # nothing to evict → no unload-now


def test_holder_is_already_target_no_swap():
    state = StateSeq({"holder": "vision", "resident": True})
    post = PostRecorder()
    res = run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep))
    assert res["swapped"] is False and post.calls == []  # already the target → just serve


def test_training_holder_is_refused_never_evicted():
    # FR-155 / SC-102: a training holder is refused; NO unload-now is sent.
    state = StateSeq({"holder": "training", "resident": True})
    post = PostRecorder()
    try:
        run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep))
    except m.PreemptRefused as e:
        assert "not preemptable" in str(e)
    else:
        raise AssertionError("expected PreemptRefused for a training holder")
    assert post.calls == []  # the training job was never told to unload


def test_unknown_serving_holder_refused_not_guessed():
    state = StateSeq({"holder": "mystery", "resident": True})
    post = PostRecorder()
    try:
        run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep))
    except m.PreemptRefused:
        pass
    else:
        raise AssertionError("expected PreemptRefused for an unmapped holder")
    assert post.calls == []  # never unload a holder we can't identify


def test_unload_now_failure_is_a_swap_error():
    state = StateSeq({"holder": "llm", "resident": True})
    post = PostRecorder(status=500, body={"error": "boom"})
    try:
        run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep))
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
        run(_preempt("llm", state_fn=state, http_post=post, sleep=_nosleep))
    except m.SwapError as e:
        assert "did not unload" in str(e)
    else:
        raise AssertionError("expected SwapError when unload-now returns a non-unloaded status")


def test_lease_never_frees_is_a_swap_error():
    # holder stays resident forever after unload-now → wait-for-free times out.
    state = StateSeq({"holder": "llm", "resident": True})  # always returns the holder
    post = PostRecorder()
    try:
        run(_preempt("vision", state_fn=state, http_post=post, sleep=_nosleep,
                                free_wait_s=1.0))
    except m.SwapError as e:
        assert "did not free" in str(e)
    else:
        raise AssertionError("expected SwapError when the lease never frees")


def test_asr_holder_evicted_via_asr_url():
    state = StateSeq({"holder": "asr", "resident": True}, {"holder": None, "resident": False})
    post = PostRecorder()
    run(_preempt("llm", state_fn=state, http_post=post, sleep=_nosleep))
    assert "8095" in post.calls[0][0]  # the ASR supervisor URL


def test_non_llm_holder_swaps_even_though_llm_resident_is_false():
    # Regression: gpu_state()'s `resident` is the LLM supervisor's OWN /health residency, so when a
    # vision/asr tenant genuinely holds the lease it reports {holder: "vision"/"asr", resident: False}
    # (Principle II → the llama-server child isn't resident). The swap must key on `holder` alone and
    # still evict — gating on `resident` here would silently no-op every non-LLM swap (back to 008).
    for holder, port in (("vision", "8092"), ("asr", "8095")):
        state = StateSeq({"holder": holder, "resident": False},   # the real gpu_state() shape
                         {"holder": None, "resident": False})       # lease freed after unload-now
        post = PostRecorder()
        res = run(_preempt("llm", state_fn=state, http_post=post, sleep=_nosleep))
        assert res["swapped"] is True and res["evicted"] == holder
        assert len(post.calls) == 1 and port in post.calls[0][0]


def test_gpu_batch_holder_is_refused_never_evicted():
    # A GPU batch drives the serving holder (llm) WITHOUT taking the training lease, so holder reads as a
    # serving tenant — but a running batch is never preempted (FR-155). Refuse before any unload-now.
    state = StateSeq({"holder": "llm", "resident": True})
    post = PostRecorder()

    async def _batch():
        return True

    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep,
                                target_probe_fn=_up, batch_active_fn=_batch))
    except m.PreemptRefused as e:
        assert "batch" in str(e).lower()
    else:
        raise AssertionError("expected PreemptRefused while a GPU batch is active")
    assert post.calls == []  # the batch-driven holder was never told to unload


def test_unreachable_target_refuses_without_evicting():
    # If the swap target daemon is down, we must NOT evict the current holder (that would drop the only
    # working serving model for a request that then 503s anyway).
    state = StateSeq({"holder": "llm", "resident": True})
    post = PostRecorder()

    async def _down(_label):
        return False

    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep,
                                target_probe_fn=_down, batch_active_fn=_nobatch))
    except m.SwapError as e:
        assert "unreachable" in str(e)
    else:
        raise AssertionError("expected SwapError when the target is unreachable")
    assert post.calls == []  # holder never evicted


def test_stale_idle_holder_is_reresolved():
    # The snapshotted holder (llm) already idle-released → its unload-now returns `idle`; the broker must
    # re-resolve and evict the REAL current holder (vision), not assume the swap is done.
    state = StateSeq({"holder": "llm", "resident": True},     # 1st resolve → llm (stale)
                     {"holder": "vision", "resident": True},  # re-resolve → vision (the real holder)
                     {"holder": None, "resident": False})     # freed after evicting vision

    class _Seq:
        def __init__(self):
            self.calls = []

        async def __call__(self, url, body):
            self.calls.append(url)
            if "8090" in url:                       # llm already released → idle
                return 200, {"status": "idle"}
            return 200, {"status": "unloaded", "drained": True}

    post = _Seq()
    res = run(m.preempt_if_needed("asr", state_fn=state, http_post=post, sleep=_nosleep,
                                  target_probe_fn=_up, batch_active_fn=_nobatch))
    assert res["swapped"] is True and res["evicted"] == "vision"
    assert len(post.calls) == 2 and "8090" in post.calls[0] and "8092" in post.calls[1]


# --- 018 US1 (FR-162): the batch guard fails CLOSED ------------------------------------------------

def test_batch_state_unknown_refuses_with_reason():
    # The seam may return a truthy *reason string* (the default probe's fail-closed contract): the
    # swap must be refused with that reason and the holder never told to unload.
    state = StateSeq({"holder": "llm", "resident": True})
    post = PostRecorder()

    async def _unknown():
        return "batch state unknown (trainer unreachable: boom) — refusing preempt (fail-closed)"

    try:
        run(m.preempt_if_needed("vision", state_fn=state, http_post=post, sleep=_nosleep,
                                target_probe_fn=_up, batch_active_fn=_unknown))
    except m.PreemptRefused as e:
        assert "batch state unknown" in str(e)
    else:
        raise AssertionError("expected PreemptRefused when the batch state is unknown")
    assert post.calls == []  # never evicted blind


def test_default_batch_probe_fails_closed_when_trainer_unreachable():
    # Drive the REAL default probe against a guaranteed-closed local port: the pre-018 behavior
    # returned False (fail-open — the one path that could evict a batch-driven serving holder);
    # 018 returns a truthy "batch state unknown" reason instead (review §4.6, FR-162).
    orig = m.TRAINER_URL
    m.TRAINER_URL = "http://127.0.0.1:9"  # discard port — nothing listens; connect fails fast
    try:
        got = run(m._default_batch_active())
    finally:
        m.TRAINER_URL = orig
    assert got, "unknown batch state must be truthy (refuse), not False (fail-open)"
    assert "batch state unknown" in str(got)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
