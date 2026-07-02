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


def test_beacon_divergent_host_fails_loud():
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        lease.verify_shared_state_dir("llama-supervisor")
        beacon = os.path.join(d, "state-dir.beacon")
        rec = json.load(open(beacon))
        rec["node"] = "some-other-distro-host"                      # recorded by a different view
        # rewrite preserves this file's OWN inode consistency, isolating the host/distro check
        with open(beacon, "w") as f:
            json.dump(rec, f)
        st = os.stat(beacon)
        rec.update(dev=st.st_dev, ino=st.st_ino)
        with open(beacon, "w") as f:
            json.dump(rec, f)
        try:
            lease.verify_shared_state_dir("trainer")
        except lease.LeaseError as e:
            assert "DIVERGES" in str(e)
        else:
            raise AssertionError("expected LeaseError on a beacon recorded by a different host")


def test_beacon_copied_file_fails_loud():
    # A beacon whose recorded inode disagrees with the file on disk is not the same file the
    # recording participant saw (copied/recreated) — two views, not one shared dir.
    with tempfile.TemporaryDirectory() as d:
        lease = _load_lease(d)
        rec = lease.verify_shared_state_dir("llama-supervisor")
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
