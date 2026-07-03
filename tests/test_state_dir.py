"""018 US1 — GPU coordination state off /tmp + shared-view startup invariant (T350, FR-166).

Offline, GPU-free: loads `serving/gpu_lease.py` fresh with a controlled environment. Two claims
are pinned: (a) the lease's DEFAULT location is the fixed state dir, never /tmp (wiped per reboot;
can diverge across mount namespaces / WSL distros — a silently-voided mutex); (b) every lease
participant verifies at startup that it observes the same coordination state as its peers
(`verify_shared_state_dir`) and fails LOUD on divergence.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEASE = os.path.join(REPO, "serving", "gpu_lease.py")


def _load_lease(state_dir, pointer=None):
    """A fresh gpu_lease module whose lease/beacon live under `state_dir`. The fixed rendezvous
    pointer is redirected per-test (default: inside `state_dir`) so tests never touch the real
    ~/.mlops-lite pointer and never collide with each other."""
    os.environ["GPU_LEASE_PATH"] = os.path.join(state_dir, "gpu.lease")
    os.environ["MLOPS_STATE_POINTER"] = pointer or os.path.join(state_dir, "state-pointer.json")
    try:
        spec = importlib.util.spec_from_file_location(f"gpu_lease_{os.path.basename(state_dir)}", LEASE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        del os.environ["GPU_LEASE_PATH"]
        del os.environ["MLOPS_STATE_POINTER"]


def test_default_lease_path_is_not_under_tmp():
    # A subprocess gives a clean module cache + env: with no overrides, the default must resolve
    # into the fixed state dir (~/.mlops-lite), NOT /tmp (review §4.6 / spec edge case).
    code = (
        "import os, sys\n"
        "os.environ.pop('GPU_LEASE_PATH', None); os.environ.pop('MLOPS_STATE_DIR', None)\n"
        f"sys.path.insert(0, {os.path.join(REPO, 'serving')!r})\n"
        "import gpu_lease\n"
        "print(gpu_lease.LEASE_PATH)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    path = out.stdout.strip()
    assert not path.startswith("/tmp/"), f"default lease path still under /tmp: {path}"
    assert path.endswith(os.path.join(".mlops-lite", "gpu.lease")), path


def test_beacon_happy_path_two_participants_same_view():
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        first = lease.verify_shared_state_dir("llama-supervisor")   # creates the beacon
        second = lease.verify_shared_state_dir("trainer")           # same view → passes
        assert first["created_by"] == "llama-supervisor"
        assert second == first


def _diverge_beacon_node(d):
    """Rewrite the beacon as if a DIFFERENT host recorded it, keeping its inode self-consistent
    so only the host/distro check trips."""
    beacon = os.path.join(d, "state-dir.beacon")
    rec = json.load(open(beacon))
    rec["node"] = "some-other-distro-host"
    with open(beacon, "w") as f:
        json.dump(rec, f)
    st = os.stat(beacon)
    rec.update(dev=st.st_dev, ino=st.st_ino)
    with open(beacon, "w") as f:
        json.dump(rec, f)


def test_beacon_divergent_host_fails_loud_while_gpu_held():
    # With a LIVE lease holder, divergence is exactly the split-brain the beacon exists to catch
    # — never self-heal over an occupied GPU.
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        lease.verify_shared_state_dir("llama-supervisor")
        lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)   # the GPU is occupied
        _diverge_beacon_node(d)
        try:
            lease.verify_shared_state_dir("trainer")
        except lease.LeaseError as e:
            assert "DIVERGES" in str(e)
        else:
            raise AssertionError("expected LeaseError on a beacon recorded by a different host")
        finally:
            lease.release("training")


def test_beacon_self_heals_when_gpu_idle():
    # Internal review (018): a reboot/hostname change tripped the identity check on EVERY daemon
    # at once — a permanent brick until an operator deleted the beacon by hand. With NO live
    # holder there is no co-residency to protect: re-stamp under the current identity and go.
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        lease.verify_shared_state_dir("llama-supervisor")
        _diverge_beacon_node(d)                                      # "the host was renamed"
        rec = lease.verify_shared_state_dir("trainer")               # idle → heals, no raise
        assert rec.get("healed_from")                                # the heal is recorded
        assert lease.verify_shared_state_dir("bento-vision")["node"] == rec["node"]  # stable now


def test_beacon_copied_file_fails_loud():
    # A beacon whose recorded inode disagrees with the file on disk is not the same file the
    # recording participant saw (copied/recreated) — two views, not one shared dir. Pinned with
    # the GPU held (idle would self-heal, above).
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        rec = lease.verify_shared_state_dir("llama-supervisor")
        lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
        beacon = os.path.join(d, "state-dir.beacon")
        rec["ino"] = rec["ino"] + 1                                  # stale identity
        with open(beacon, "w") as f:
            json.dump(rec, f)
        # rewriting changed the inode? not necessarily — force mismatch is already in rec["ino"]
        try:
            lease.verify_shared_state_dir("trainer")
        except lease.LeaseError as e:
            assert "identity changed" in str(e) or "DIVERGES" in str(e)
        else:
            raise AssertionError("expected LeaseError on a copied/recreated beacon")
        finally:
            lease.release("training")


def test_recycled_pid_stale_record_is_reclaimed_not_adopted():
    # Internal review (018): the same-pid branch trusted the PID alone. A stale record whose PID
    # was recycled to THIS process was returned as "already ours" (same tenant) or refused as a
    # live holder (different tenant) — while peers, seeing the start-time mismatch, would reclaim
    # the file out from under us. The start-time check makes it what it is: a dead holder.
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        stale = {"tenant": "llm-serving", "pid": os.getpid(), "pid_start": 12345,  # not OUR start
                 "acquired_at": 1.0}
        with open(os.path.join(d, "gpu.lease"), "w") as f:
            json.dump(stale, f)
        rec = lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)  # reclaims, never Held
        assert rec["tenant"] == "training" and rec["pid_start"] != 12345  # a FRESH claim
        lease.release("training")
        # same-tenant flavor: a stale same-pid record must yield a fresh claim too, not the
        # stale record echoed back as if it were live
        with open(os.path.join(d, "gpu.lease"), "w") as f:
            json.dump(dict(stale, tenant="training"), f)
        rec = lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
        assert rec["pid_start"] != 12345
        lease.release("training")


def test_live_legacy_tmp_holder_blocks_a_fresh_claim():
    # Internal review (018): during a mixed-version upgrade an OLD daemon still arbitrates via
    # the pre-018 /tmp lease file and cannot see ours — the new side must honor a LIVE holder
    # there before claiming, or the two domains each admit a tenant.
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        legacy_path = lease._LEGACY_TMP_LEASE          # conftest points this at a temp file
        os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
        live = {"tenant": "llm-serving", "pid": os.getpid(),
                "pid_start": lease._proc_start(os.getpid()), "acquired_at": 1.0}
        with open(legacy_path, "w") as f:
            json.dump(live, f)
        try:
            lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
        except lease.LeaseHeld as e:
            assert "legacy" in str(e.holder.get("domain", ""))
        else:
            raise AssertionError("a live legacy /tmp holder must block a fresh claim")
        finally:
            os.unlink(legacy_path)
        # a DEAD legacy record never blocks (self-healing stays intact across the domains)
        with open(legacy_path, "w") as f:
            json.dump({"tenant": "llm-serving", "pid": 2 ** 22 + 12345, "pid_start": 1,
                       "acquired_at": 1.0}, f)
        try:
            lease.acquire("training", est_gb=1.0, vram_budget_gb=12.0)
            lease.release("training")
        finally:
            os.unlink(legacy_path)


def test_same_host_divergent_state_dirs_fail_loud_at_the_pointer():
    # Codex review (018): two daemons on ONE host pointed at DIFFERENT state dirs each create a
    # self-consistent beacon in their private dir — the per-dir beacon cannot catch that. The
    # fixed rendezvous pointer does: the second daemon must fail loud, not run a private mutex.
    with tempfile.TemporaryDirectory() as pointer_home, \
            tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        pointer = os.path.join(pointer_home, "state-pointer.json")
        lease_a = _load_lease(dir_a, pointer=pointer)
        lease_a.verify_shared_state_dir("llama-supervisor")     # registers dir_a at the pointer
        lease_b = _load_lease(dir_b, pointer=pointer)           # a typo'd MLOPS_STATE_DIR
        try:
            lease_b.verify_shared_state_dir("trainer")
        except lease_b.LeaseError as e:
            assert "MISMATCH" in str(e) and pointer in str(e)
        else:
            raise AssertionError("expected LeaseError for a same-host private state dir")
        # the honest participant still verifies cleanly against the same pointer
        lease_a.verify_shared_state_dir("bento-vision")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
