"""017 US2 — training is never preempted (T335, FR-155, SC-102).

A `preempt=true` serving request against a **training/HPO/batch** holder MUST be refused — the job is
never evicted. This drives both the orchestration core (`preempt_if_needed` → `PreemptRefused`, no
`unload-now` sent) and the router-facing wrapper (`preempt_or_409` → FastAPI 409). Offline, GPU-free.
"""
import asyncio
import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_swap():
    spec = importlib.util.spec_from_file_location(
        "swap_under_test_us2", os.path.join(REPO, "gateway", "app", "swap.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load_swap()


async def _training_state():
    return {"holder": "training", "resident": True}


class _RecordingPost:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, body):
        self.calls.append((url, body))
        return 200, {"status": "unloaded"}


async def _nosleep(_):
    return None


def test_training_holder_refused_and_no_unload_sent():
    post = _RecordingPost()
    try:
        asyncio.run(m.preempt_if_needed("vision", state_fn=_training_state, http_post=post,
                                        sleep=_nosleep))
    except m.PreemptRefused as e:
        assert "not preemptable" in str(e)
    else:
        raise AssertionError("expected PreemptRefused for a training holder (SC-102)")
    assert post.calls == []  # the training job is never told to unload (never evicted)


def test_router_wrapper_maps_training_refusal_to_409():
    import pytest
    HTTPException = pytest.importorskip("fastapi").HTTPException  # skip cleanly if the gateway dep is absent
    post = _RecordingPost()
    try:
        asyncio.run(m.preempt_or_409("vision", state_fn=_training_state, http_post=post, sleep=_nosleep))
    except HTTPException as e:
        assert e.status_code == 409 and "not preemptable" in str(e.detail)
    else:
        raise AssertionError("expected a 409 HTTPException for a training holder")
    assert post.calls == []


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
