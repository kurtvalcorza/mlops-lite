"""018 US2 — in-process single-slot admission (T353/T357, FR-168/175).

Offline, GPU-free: drives `hostagent.admission` with an injected GPU reader and no lockfile
interop. Pins: single-slot exclusivity under a thread hammer (no TOCTOU by construction),
same-tenant idempotent re-acquire (no re-check), live-VRAM refusal, static-budget fallback when
the GPU is unreadable, and the TTL cache (steady-state reads don't hit the reader).
"""
import os
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402


def _adm(free_gb=10.0, budget=12.0, reads=None):
    def read():
        if reads is not None:
            reads.append(1)
        return free_gb

    return adm.Admission(vram_budget_gb=budget,
                         gpu=adm.GpuReader(ttl_s=1000.0, read_fn=read))


def test_single_slot_thread_hammer_no_toctou():
    a = _adm()
    winners, refused = [], []
    barrier = threading.Barrier(16)

    def contend(i):
        barrier.wait()  # maximize the window: all 16 decide "is it free?" together
        try:
            a.acquire(f"tenant-{i}", "serving", est_gb=1.0)
            winners.append(i)
        except adm.Held:
            refused.append(i)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and len(refused) == 15  # exactly one admission, ever
    assert a.holder()["tenant"] == f"tenant-{winners[0]}"


def test_same_tenant_reacquire_is_idempotent_no_readmission():
    reads = []
    a = _adm(free_gb=10.0, reads=reads)
    a.acquire("llm", "serving", est_gb=8.0)
    first_reads = len(reads)
    # With the model resident, free VRAM is low — a re-check would wrongly evict us. The
    # same-tenant path must return the existing claim WITHOUT re-running admission.
    a.gpu._read = lambda: 0.5  # resident model ate the VRAM
    again = a.acquire("llm", "serving", est_gb=8.0)
    assert again["tenant"] == "llm"
    assert len(reads) == first_reads  # no fresh admission read on the idempotent path


def test_vram_refusal_and_static_fallback():
    a = _adm(free_gb=2.0)
    try:
        a.acquire("llm", "serving", est_gb=4.0)
    except adm.VramExceeded:
        pass
    else:
        raise AssertionError("expected VramExceeded at 4GB est vs 2GB free")
    assert a.holder() is None  # a refused admission must not leave a claim behind

    unreadable = adm.Admission(vram_budget_gb=12.0,
                               gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: None))
    unreadable.acquire("llm", "serving", est_gb=8.0)          # 8 < 12*0.95 → admitted
    unreadable.release("llm")
    try:
        unreadable.acquire("llm", "serving", est_gb=11.9)     # > 12*0.95 → refused
    except adm.VramExceeded as e:
        assert "static" in str(e)
    else:
        raise AssertionError("expected the static-budget fallback to refuse (never fail-open)")


def test_release_is_idempotent_and_own_tenant_only():
    a = _adm()
    a.acquire("llm", "serving", est_gb=1.0)
    a.release("vision")                      # not the holder — must be a no-op
    assert a.holder()["tenant"] == "llm"
    a.release("llm")
    a.release("llm")                         # idempotent
    assert a.holder() is None
    a.acquire("vision", "serving", est_gb=1.0)  # the slot is genuinely free again
    assert a.holder()["tenant"] == "vision"


def test_ttl_cache_bounds_reader_calls():
    reads = []
    clock = {"t": 0.0}
    reader = adm.GpuReader(ttl_s=1.0, clock=lambda: clock["t"],
                           read_fn=lambda: (reads.append(1), 10.0)[1])
    for _ in range(50):                      # steady-state polling within the TTL window
        assert reader.free_gb() == 10.0
    assert len(reads) == 1                   # one read serves them all (FR-175 / SC-110)
    clock["t"] = 2.0
    reader.free_gb()
    assert len(reads) == 2                   # a fresh read after the TTL elapses
    reader.free_gb(fresh=True)
    assert len(reads) == 3                   # admission always forces a fresh read


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
