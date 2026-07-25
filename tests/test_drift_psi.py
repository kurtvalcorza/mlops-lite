"""024 US5 (T588) — web-free unit test for the input-drift PSI math (`gateway/app/monitoring.py:psi`).

The Population Stability Index is pure arithmetic (reference deciles as bin edges), but it had no direct
offline coverage — only the end-to-end `compute_drift` path over a live store exercised it. Pin the math
itself here, no live stack: identical distributions ~0, a shifted distribution a clear positive score,
and the degenerate/empty inputs handled without a crash (FR-346/SC-173).
"""
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Import as `app.monitoring` (gateway/ on path) — the canonical gateway-suite import — NOT
# `gateway.app.monitoring`, which would be a SECOND module object that re-registers the module-level
# `gateway_drift_score` prometheus Gauge and collides ("Duplicated timeseries") with the real one.
for _p in (REPO, os.path.join(REPO, "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.monitoring import psi  # noqa: E402

_REF = list(range(1, 101))  # 1..100


def test_identical_distributions_score_zero():
    assert abs(psi(_REF, list(_REF))) < 1e-9          # r == c in every bucket → each term 0


def test_fully_shifted_distribution_scores_high():
    shifted = list(range(201, 301))                   # entirely above every reference bin edge
    score = psi(_REF, shifted)
    assert math.isfinite(score) and score > 1.0       # a large, unmistakable drift


def test_larger_shift_scores_higher_than_a_small_one():
    small = [x + 5 for x in _REF]                     # nudged within range
    large = [x + 80 for x in _REF]                    # pushed off the top of the reference range
    assert psi(_REF, small) < psi(_REF, large)


def test_degenerate_reference_returns_zero():
    # Fewer than two reference points ⇒ no meaningful edges ⇒ 0.0 (documented guard, not a crash).
    assert psi([5], [1, 2, 3]) == 0.0
    assert psi([], [1, 2, 3]) == 0.0


def test_empty_current_is_finite_and_positive():
    # An empty current window vs a populated reference is maximal drift — must be a finite number,
    # never a ZeroDivisionError or inf (dist() divides by `len(data) or 1`, log has an eps floor).
    score = psi(_REF, [])
    assert math.isfinite(score) and score > 0.0


def test_both_empty_is_zero():
    assert psi([], []) == 0.0                          # degenerate reference guard wins first


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
