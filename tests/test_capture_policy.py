"""016 US1 — bounded recoverable-input capture policy (T306, FR-146/147, SC-094).

Offline + I/O-free for the policy core: `should_capture` (sampling admission) and `inputs_to_prune`
(cap + TTL eviction) are pure and unit-test directly. The fire-and-forget `capture_input` storage is
driven against the in-memory FakeS3 with a synchronous-thread shim (deterministic, no sleeps): capture
on → a recoverable input is stored under `inputs/<modality>/`; capture off (or a non-replayable modality)
→ nothing; the per-modality cap is enforced by the prune-on-write.
"""
import io
import os
import sys
import threading as _real_threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _quality import FakeS3, load_quality  # noqa: E402


class _ThreadingShim:
    """A `threading` stand-in: SyncThread for Thread, everything else delegates to the real module. Set as
    the loaded quality module's `threading` so capture runs synchronously WITHOUT mutating the process-wide
    `threading.Thread` (which would leak into other tests — e.g. 017's unload-now drain uses real threads)."""

    Thread = None  # set to SyncThread once it's defined below

    def __getattr__(self, name):
        return getattr(_real_threading, name)


class FakeS3Del(FakeS3):
    """FakeS3 + delete_object (the prune path) — keeps the in-memory bucket faithful to the prune."""

    def delete_object(self, Bucket, Key):
        self.objs.pop(Key, None)


class SyncThread:
    """A threading.Thread stand-in that runs the target synchronously — makes the fire-and-forget
    capture deterministic in tests (no sleeps, no races)."""

    def __init__(self, target=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


_ThreadingShim.Thread = SyncThread


def _wire(mod, s3, **flags):
    mod._s3 = lambda: s3
    mod.threading = _ThreadingShim()  # rebind the module's ref — do NOT mutate global threading.Thread
    mod.QUALITY_LOGGING_ENABLED = flags.get("logging", True)
    mod.QUALITY_CAPTURE_IO = flags.get("capture", True)
    mod.SHADOW_CAPTURE_SAMPLE = flags.get("sample", 1.0)
    mod.SHADOW_CAPTURE_CAP_N = flags.get("cap_n", 500)
    mod.SHADOW_CAPTURE_TTL_S = flags.get("ttl_s", 7 * 24 * 3600)


# --- pure policy ----------------------------------------------------------------------------------

def test_should_capture_sampling_bounds():
    m = load_quality()
    assert m.should_capture(sample=1.0) is True          # capture all
    assert m.should_capture(sample=0.0) is False          # capture none
    assert m.should_capture(sample=0.5, roll=0.4) is True   # roll under rate → captured
    assert m.should_capture(sample=0.5, roll=0.6) is False  # roll over rate → skipped


def test_inputs_to_prune_ttl_and_cap():
    m = load_quality()
    now = 1000.0
    recs = [("k_old", 100.0), ("k1", 990.0), ("k2", 995.0), ("k3", 998.0), ("k4", 999.0)]
    # TTL 50s → k_old (age 900) expires; cap 2 → keep newest 2 (k4, k3), prune k1, k2.
    to_del = m.inputs_to_prune(recs, now=now, cap_n=2, ttl_s=50)
    assert "k_old" in to_del and "k1" in to_del and "k2" in to_del
    assert "k3" not in to_del and "k4" not in to_del


def test_inputs_to_prune_no_cap_no_ttl_keeps_all():
    m = load_quality()
    recs = [("a", 1.0), ("b", 2.0)]
    assert m.inputs_to_prune(recs, now=100.0, cap_n=0, ttl_s=0) == []


def test_input_key_roundtrips():
    m = load_quality()
    key = m._input_key("image-classification", "pid123", 1700000000.5)
    mod, ts, pid = m.parse_input_key(key)
    assert mod == "image-classification" and pid == "pid123" and abs(ts - 1700000000.5) < 0.01


# --- storage (fire-and-forget, deterministic via SyncThread) --------------------------------------

def test_capture_on_stores_recoverable_input():
    m = load_quality()
    s3 = FakeS3Del()
    _wire(m, s3, capture=True, sample=1.0)
    m.capture_input("pidA", "vision", "BASE64IMAGE")
    keys = [k for k in s3.objs if k.startswith("inputs/image-classification/")]
    assert len(keys) == 1
    import json
    rec = json.loads(s3.objs[keys[0]])
    assert rec["input"] == "BASE64IMAGE" and rec["prediction_id"] == "pidA"


def test_capture_off_stores_nothing():
    m = load_quality()
    s3 = FakeS3Del()
    _wire(m, s3, capture=False)
    m.capture_input("pidB", "vision", "BASE64IMAGE")
    assert not any(k.startswith("inputs/") for k in s3.objs)  # privacy default preserved (SC-094)


def test_non_replayable_modality_not_captured():
    m = load_quality()
    s3 = FakeS3Del()
    _wire(m, s3, capture=True)
    m.capture_input("pidC", "embedding", "vec-input")  # embeddings are out of shadow-replay scope
    assert not any(k.startswith("inputs/") for k in s3.objs)


def test_sample_zero_captures_nothing():
    m = load_quality()
    s3 = FakeS3Del()
    _wire(m, s3, capture=True, sample=0.0)
    m.capture_input("pidD", "asr", "AUDIOB64")
    assert not any(k.startswith("inputs/") for k in s3.objs)


def test_cap_enforced_on_write():
    m = load_quality()
    s3 = FakeS3Del()
    _wire(m, s3, capture=True, sample=1.0, cap_n=3)
    for i in range(6):
        m.capture_input(f"pid{i}", "asr", f"audio{i}")
    keys = [k for k in s3.objs if k.startswith("inputs/asr/")]
    assert len(keys) <= 3  # the prune keeps at most cap_n newest


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
