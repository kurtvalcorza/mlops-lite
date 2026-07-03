"""018 US2 — migration interop: agent admission ⇄ legacy lockfile lease (T353/T357, FR-166/168).

Offline, GPU-free: a fresh `gpu_lease` module over a temp state dir stands in for the legacy
world. Pins mutual exclusion ACROSS the migration boundary, both directions: a legacy tenant
(e.g. the not-yet-folded trainer) blocks agent admission, and an agent tenant blocks a legacy
acquire — one GPU tenant at every instant of every phase (the spec's hard boundary), including
the legacy-identity mapping (agent "llm" claims the lockfile as "llm-serving").
"""
import contextlib
import importlib.util
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402


@contextlib.contextmanager
def _as_pid(pid):
    """Run a lease call as if from a DIFFERENT process (a separate legacy daemon). The lease keys
    ownership on os.getpid(); a real legacy daemon has its own PID, so simulating one in-process
    means claiming/refusing under a different PID rather than the test's own."""
    real = os.getpid
    os.getpid = lambda: pid
    try:
        yield
    finally:
        os.getpid = real


def _load_lease(state_dir):
    os.environ["GPU_LEASE_PATH"] = os.path.join(state_dir, "gpu.lease")
    try:
        spec = importlib.util.spec_from_file_location(
            f"gpu_lease_interop_{os.path.basename(state_dir)}",
            os.path.join(REPO, "serving", "gpu_lease.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        del os.environ["GPU_LEASE_PATH"]


def _agent(lease):
    return adm.Admission(vram_budget_gb=12.0,
                         gpu=adm.GpuReader(ttl_s=1000.0, read_fn=lambda: 10.0), lease=lease)


def test_legacy_tenant_blocks_agent_admission():
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        with _as_pid(1):                       # the un-folded trainer — a SEPARATE live process
            lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
        a = _agent(lease)
        try:
            a.acquire("llm", "serving", est_gb=1.0)
        except adm.Held as e:
            # Mapped to the AGENT'S exception type (internal review, 018): the raw
            # gpu_lease.LeaseHeld is a foreign class the HTTP surface doesn't map — it would
            # surface as a 500 instead of the contracted 409. The holder details survive.
            assert e.holder.get("tenant") == "training"
        else:
            raise AssertionError("agent admission must respect a legacy lockfile holder")
        assert a.holder() is None            # the failed interop claim left no agent-side state
        with _as_pid(1):
            lease.release("training")
        a.acquire("llm", "serving", est_gb=1.0)   # frees up → admission proceeds
        assert a.holder()["tenant"] == "llm"


def test_agent_tenant_blocks_legacy_acquire_under_legacy_identity():
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        a = _agent(lease)
        a.acquire("llm", "serving", est_gb=1.0)
        holder = lease.current_holder()
        assert holder and holder["tenant"] == "llm-serving"   # the legacy identity mapping
        with _as_pid(1):                       # a legacy tenant's acquire — a different process
            try:
                lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
            except lease.LeaseHeld:
                pass
            else:
                raise AssertionError("a legacy tenant must not co-reside with an agent tenant")
        a.release("llm")
        assert lease.current_holder() is None                 # interop release frees the file too
        with _as_pid(1):
            lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
            lease.release("training")


def test_dropped_release_self_heals_same_owner_tenant_switch():
    """019/US2 (FR-190): a release whose lockfile step was dropped (admission.release swallows a
    failed lease.release) leaves a stale record owned by THIS live PID. The next acquire for a
    different tenant MUST reclaim our own stale record, not wedge behind it — a different live PID
    is still refused (covered by the two tests above)."""
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        a = _agent(lease)
        a.acquire("llm", "serving", est_gb=1.0)
        assert lease.current_holder()["tenant"] == "llm-serving"

        a._holder = None                       # in-process release ran; lockfile release was dropped
        assert lease.current_holder()["tenant"] == "llm-serving"   # our stale record still on disk

        a.acquire("vision", "serving", est_gb=1.0)   # must self-heal our own stale claim, no restart
        assert a.holder()["tenant"] == "vision"
        assert lease.current_holder()["tenant"] == "vision"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
