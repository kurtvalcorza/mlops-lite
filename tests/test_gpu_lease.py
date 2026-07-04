"""SC-042/SC-043 — the one-GPU-tenant guarantee survives the lockfile's retirement (T364, offline).

008's cross-process file lease (`serving/gpu_lease.py`) enforced "one GPU tenant" with a PID-stamped
lockfile + fcntl coordination, because the tenants were separate processes. T364 retired it: with
every tenant folded into ONE process (`hostagent`), the guarantee is a decision under
`hostagent.admission`'s single re-entrant lock — race-free by construction, no time-of-check/
time-of-use window. This test re-anchors the retired lease's HEADLINE guarantees on the admission
surface (SC-042 one-tenant, SC-043 VRAM admission), plus the in-process mechanism that REPLACED the
lockfile's cross-process coordination: the swap RESERVATION (while a swap is in flight it reserves
the freed slot, so only its target may claim it — the window a third tenant could otherwise snipe).

The fine-grained admission unit cases (idempotent re-acquire, TTL cache, release semantics) live in
`test_agent_admission.py`; the swap transaction in `test_agent_swap_txn.py`. This module is the
lockfile-retirement regression anchor. Pure stdlib threading — portable (the old lockfile test
needed fcntl + fork, so it was WSL-only; the successor is not).
"""
import os
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402


def _adm(free_gb=10.0, budget=12.0):
    return adm.Admission(vram_budget_gb=budget,
                         gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: free_gb))


def test_second_gpu_tenant_refused_principle_ii():
    """SC-042: a second tenant can never co-hold the single slot (the old lease's LeaseHeld)."""
    a = _adm()
    a.acquire("llm", "serving", est_gb=1.0)
    try:
        a.acquire("vision", "serving", est_gb=1.0)
    except adm.Held as e:
        assert "Principle II" in str(e) and e.holder["tenant"] == "llm"
    else:
        raise AssertionError("a second GPU tenant must be refused while llm holds the slot")
    assert a.holder()["tenant"] == "llm"


def test_concurrent_acquire_admits_exactly_one_no_toctou():
    """SC-042: the race the two old HTTP guards had — 12 threads decide "is it free?" together and
    exactly one is admitted (the claim is atomic under the admission lock)."""
    a = _adm()
    winners = []
    barrier = threading.Barrier(12)

    def contend(i):
        barrier.wait()
        try:
            a.acquire(f"t{i}", "serving", est_gb=1.0)
            winners.append(i)
        except adm.Held:
            pass

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and a.holder()["tenant"] == f"t{winners[0]}"


def test_swap_reservation_excludes_non_target():
    """The in-process replacement for the lockfile's cross-process coordination: a swap reserves the
    freed slot for its target, so a third tenant cannot snipe it mid-transaction — only the target may
    claim it, and end_swap releases the reservation."""
    a = _adm()
    a.begin_swap("vision")                       # a swap transaction reserves the slot for vision
    try:
        a.acquire("asr", "serving", est_gb=1.0)  # a non-target must not win the reserved slot
    except adm.Held as e:
        assert e.holder["kind"] == "swap-reservation"
    else:
        raise AssertionError("a reserved slot must refuse a non-target tenant")
    assert a.holder() is None                    # reserved, not yet held
    a.acquire("vision", "serving", est_gb=1.0)   # the reservation's target may claim it
    assert a.holder()["tenant"] == "vision"
    a.end_swap("vision")


def test_oversize_load_refused_live_then_static_fallback():
    """SC-043: an oversize estimate is refused against live free VRAM, and — when the GPU is
    unreadable — against the static budget (never fail-open into co-residency)."""
    a = _adm(free_gb=2.0)
    try:
        a.acquire("llm", "serving", est_gb=8.0)
    except adm.VramExceeded:
        pass
    else:
        raise AssertionError("oversize load must be refused against live free VRAM")
    assert a.holder() is None                     # a refused admission leaves no claim behind

    blind = adm.Admission(vram_budget_gb=12.0,
                          gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: None))
    try:
        blind.acquire("llm", "serving", est_gb=11.9)  # > 12 * 0.95 → refused
    except adm.VramExceeded as e:
        assert "static" in str(e)
    else:
        raise AssertionError("the static-budget fallback must refuse an oversize load")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
