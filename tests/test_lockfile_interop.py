"""018 US2 — migration interop: agent admission ⇄ legacy lockfile lease (T353/T357, FR-166/168).

Offline, GPU-free: a fresh `gpu_lease` module over a temp state dir stands in for the legacy
world. Pins mutual exclusion ACROSS the migration boundary, both directions: a legacy tenant
(e.g. the not-yet-folded trainer) blocks agent admission, and an agent tenant blocks a legacy
acquire — one GPU tenant at every instant of every phase (the spec's hard boundary), including
the legacy-identity mapping (agent "llm" claims the lockfile as "llm-serving").
"""
import importlib.util
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hostagent import admission as adm  # noqa: E402


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
        lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)  # the un-folded trainer
        a = _agent(lease)
        try:
            a.acquire("llm", "serving", est_gb=1.0)
        except lease.LeaseHeld:
            pass
        else:
            raise AssertionError("agent admission must respect a legacy lockfile holder")
        assert a.holder() is None            # the failed interop claim left no agent-side state
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
        try:
            lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
        except lease.LeaseHeld:
            pass
        else:
            raise AssertionError("a legacy tenant must not co-reside with an agent tenant")
        a.release("llm")
        assert lease.current_holder() is None                 # interop release frees the file too
        lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
        lease.release("training")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
