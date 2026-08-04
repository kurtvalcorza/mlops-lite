"""026 T622 — characterization of the PRE-redesign single-slot `hostagent.admission`.

Written before `hostagent/coordinator.py` exists, and referenced by T634: these tests record what
the single-slot lease actually does today so the redesign's behavioural deltas are *chosen* rather
than discovered later. `tests/test_agent_admission.py` covers the same module from the 018
requirements side; this file is deliberately about observed behaviour, including the parts nobody
would have specified on purpose.

**These tests are expected to keep passing.** The coordinator does not replace `Admission` in place —
`hostagent.swap` and the 018 lease paths still use it (T632 keeps the single-resident serving path
byte-compatible), so a change here is a regression, not a migration. The intentional deltas the
coordinator introduces are recorded in `test_agent_coordinator.py` and named in the docstring below.

Documented deltas the coordinator (T634+) intends, each of which this file pins the OLD side of:

  1. **Single slot -> resident set.** `acquire` refuses a second tenant with `Held`; the coordinator
     admits it when both VRAM bounds allow (`test_second_tenant_is_refused_regardless_of_headroom`).
  2. **`est > free` with no eviction.** `Admission` refuses on live-free alone and never evicts to
     make room; the coordinator runs evict-then-recompute (`test_refuses_when_estimate_exceeds_free`).
  3. **One bound, not two.** `Admission` compares the estimate against live free VRAM *or* the
     static budget, never both, and has no notion of outstanding reservations
     (`test_static_budget_fallback_when_gpu_unreadable`).
  4. **No ref-counting.** `release` drops the claim unconditionally; there is no `active_requests`,
     so nothing waits for in-flight work (`test_release_is_immediate_and_unconditional`).
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


# -- delta 1: the slot is single, and headroom does not change that ---------------------------------

def test_second_tenant_is_refused_regardless_of_headroom():
    """10 GB free, two 1 GB tenants: they would trivially co-reside under the coordinator's bounds.
    The single slot refuses the second anyway — occupancy, not capacity, is what it checks."""
    a = _adm(free_gb=10.0)
    a.acquire("alice", "serving", est_gb=1.0)
    try:
        a.acquire("bob", "serving", est_gb=1.0)
        raise AssertionError("expected Held — the slot admits exactly one tenant")
    except adm.Held as e:
        assert e.holder["tenant"] == "alice"


def test_same_tenant_reacquire_returns_the_existing_claim_without_rechecking():
    """The idempotent re-affirm: re-running VRAM admission against your own resident model would
    see the low free VRAM your model caused and evict you. Pinned because the coordinator reaches
    the same conclusion by a different route (a `Share` on an already-`resident` entry)."""
    reads = []

    def read():
        reads.append(1)
        return 10.0

    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=read))
    first = a.acquire("alice", "serving", est_gb=9.5)
    before = len(reads)
    again = a.acquire("alice", "serving", est_gb=9.5)
    assert again == first
    assert len(reads) == before, "a same-tenant re-acquire must not re-read the GPU"


# -- delta 2/3: one bound, and no eviction ----------------------------------------------------------

def test_refuses_when_estimate_exceeds_free_and_never_evicts():
    """`VramExceeded`, and the holder is untouched — there is no path here that frees room."""
    a = _adm(free_gb=2.0)
    try:
        a.acquire("alice", "serving", est_gb=4.0)
        raise AssertionError("expected VramExceeded")
    except adm.VramExceeded:
        pass
    assert a.holder() is None, "a refused admission leaves no claim behind"


def test_live_free_is_the_only_bound_when_the_gpu_is_readable():
    """A 9.9 GB estimate against 10 GB free is admitted even though it exceeds the 12 GB budget's
    safety margin — the budget check applies ONLY in the unreadable-GPU fallback."""
    a = _adm(free_gb=10.0, budget=12.0)
    assert a.acquire("alice", "serving", est_gb=9.9)["tenant"] == "alice"


def test_static_budget_fallback_when_gpu_unreadable():
    """Unreadable GPU -> the static budget * 0.95, never fail-open into co-residency."""
    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: None))
    assert a.acquire("alice", "serving", est_gb=11.0)["tenant"] == "alice"
    a.release("alice")
    try:
        a.acquire("bob", "serving", est_gb=11.5)  # > 12 * 0.95
        raise AssertionError("expected VramExceeded from the static-budget check")
    except adm.VramExceeded:
        pass


def test_admission_reads_the_gpu_fresh_for_a_real_admission():
    """A real admission bypasses the TTL cache; queries do not. Pinned because the coordinator
    keeps this split (stage 1 reads live-free fresh, `GET /admin/queue` reads cached)."""
    reads = []

    def read():
        reads.append(1)
        return 10.0

    a = adm.Admission(vram_budget_gb=12.0, gpu=adm.GpuReader(ttl_s=1000.0, read_fn=read))
    a.free_gb()
    a.free_gb()
    assert len(reads) == 1, "steady-state queries hit the TTL cache"
    a.acquire("alice", "serving", est_gb=1.0)
    assert len(reads) == 2, "a real admission forces a fresh read"


# -- delta 4: release is unconditional; nothing is ref-counted ---------------------------------------

def test_release_is_immediate_and_unconditional():
    """No drain, no in-flight-request wait, no ref-count — the claim is simply gone. The
    coordinator's `evict()` instead drains on `active_requests == 0` before it unloads anything."""
    a = _adm()
    a.acquire("alice", "serving", est_gb=1.0)
    a.release("alice")
    assert a.holder() is None
    assert a.acquire("bob", "serving", est_gb=1.0)["tenant"] == "bob"


def test_release_by_a_non_holder_is_a_no_op():
    a = _adm()
    a.acquire("alice", "serving", est_gb=1.0)
    a.release("bob")
    assert a.holder()["tenant"] == "alice", "only the holder can drop the holder's claim"


# -- the swap reservation (FR-171) — the coordinator's `job_barrier` is its descendant ---------------

def test_swap_reservation_admits_only_its_target():
    a = _adm()
    a.begin_swap("bob")
    try:
        a.acquire("alice", "serving", est_gb=1.0)
        raise AssertionError("expected Held — the reservation owns the freed slot")
    except adm.Held as e:
        assert e.holder["kind"] == "swap-reservation"
    assert a.acquire("bob", "serving", est_gb=1.0)["tenant"] == "bob"


def test_a_second_swap_is_refused_even_for_the_same_target():
    """019/US6 FR-194: same-target concurrency was the bug — two swaps shared `_swap_target` and the
    first to finish dropped the reservation out from under the second."""
    a = _adm()
    a.begin_swap("bob")
    try:
        a.begin_swap("bob")
        raise AssertionError("expected Held — swaps are single-flight")
    except adm.Held:
        pass


def test_retarget_swap_hands_the_window_over_without_dropping_it():
    """The rollback path: the target failed to load after the holder was evicted, so the freed
    window must pass to the evicted holder with no gap a contender could snipe."""
    a = _adm()
    a.begin_swap("bob")
    a.retarget_swap("bob", "alice")
    try:
        a.acquire("bob", "serving", est_gb=1.0)
        raise AssertionError("expected Held — the reservation now belongs to alice")
    except adm.Held:
        pass
    assert a.acquire("alice", "serving", est_gb=1.0)["tenant"] == "alice"


# -- the property the redesign must not lose --------------------------------------------------------

def test_no_toctou_under_a_thread_hammer():
    """Sixteen threads decide together; exactly one is admitted. The coordinator must preserve this
    (its stage-1 critical section is the same guarantee, widened to a set)."""
    a = _adm()
    winners, refused = [], []
    barrier = threading.Barrier(16)

    def contend(i):
        barrier.wait()
        try:
            a.acquire(f"tenant-{i}", "serving", est_gb=1.0)
            winners.append(i)
        except adm.Held:
            refused.append(i)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and len(refused) == 15


def test_set_child_records_the_vram_owning_pid_for_the_holder_only():
    a = _adm()
    a.acquire("alice", "serving", est_gb=1.0)
    a.set_child("bob", 4242)
    assert a.holder()["child_pid"] is None
    a.set_child("alice", 4242)
    assert a.holder()["child_pid"] == 4242


def main() -> int:
    """Standalone entry point, matching the suite's convention."""
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001 — a standalone runner reports every failure
                traceback.print_exc()
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
