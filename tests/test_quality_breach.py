"""013 US3 — quality breach + combine policy (T250 / SC-079).

Offline coverage of the breach decision (relative-to-baseline, direction-aware) and the OR+cooldown
combine policy that debounces the two retrain signals. The retrain *launch* itself (httpx → trainer)
is exercised in the live integration leg; here the gating logic is pinned.
"""
import sys

from _quality import load_quality

q = load_quality()


# --- relative-to-baseline breach, both directions -------------------------------------------------

def test_higher_better_breach_threshold():
    # accuracy: breach when value drops > drop_pct below baseline.
    assert q.is_breach(0.80, 0.95, "higher", 0.10) is True    # 0.80 < 0.95*(1-0.10)=0.855
    assert q.is_breach(0.90, 0.95, "higher", 0.10) is False   # within tolerance
    assert q.is_breach(0.99, 0.95, "higher", 0.10) is False   # improved


def test_lower_better_breach_threshold():
    # WER/perplexity: breach when value RISES > drop_pct above baseline (direction inverts the test).
    assert q.is_breach(0.25, 0.20, "lower", 0.10) is True     # 0.25 > 0.20*(1+0.10)=0.22
    assert q.is_breach(0.21, 0.20, "lower", 0.10) is False    # within tolerance
    assert q.is_breach(0.10, 0.20, "lower", 0.10) is False    # improved


def test_no_baseline_or_no_value_never_breaches():
    assert q.is_breach(0.5, None, "higher", 0.10) is False    # nothing to compare against
    assert q.is_breach(None, 0.9, "higher", 0.10) is False    # insufficient data


def test_configurable_drop_pct():
    assert q.is_breach(0.90, 0.95, "higher", 0.01) is True    # tight 1% tolerance bites
    assert q.is_breach(0.90, 0.95, "higher", 0.10) is False   # lax 10% tolerance forgives


# --- OR + cooldown combine policy -----------------------------------------------------------------

def test_cooldown_debounces_a_recent_retrain():
    q.reset_cooldown()
    assert q.in_cooldown() is False                            # nothing fired yet
    q.note_retrain(now=1000.0)
    assert q.in_cooldown(now=1100.0, cooldown_sec=3600) is True   # within the window → debounced
    assert q.in_cooldown(now=5000.0, cooldown_sec=3600) is False  # window elapsed → free again
    q.reset_cooldown()


def test_cooldown_is_shared_across_signals():
    # the policy is OR + cooldown: a retrain from EITHER signal starts the same clock, so the other
    # signal is debounced. note_retrain() is the single shared gate both triggers call.
    q.reset_cooldown()
    q.note_retrain(now=500.0)                                  # e.g. the input-PSI signal fired
    assert q.in_cooldown(now=600.0, cooldown_sec=1000) is True # the quality signal must now debounce
    q.reset_cooldown()


def test_evaluate_quality_breach_flag_drives_the_trigger():
    # the report's breach flag is what the router gates the retrain on — confirm it reflects the
    # baseline comparison end-to-end.
    bad = [{"prediction": "x", "label": "cat", "ts": i} for i in range(25)]   # 0.0 accuracy
    good = [{"prediction": "cat", "label": "cat", "ts": i} for i in range(25)]  # 1.0 accuracy
    rbad = q.evaluate_quality(bad, modality="vision", model_version="3", baseline=0.95, min_pairs=10)
    rgood = q.evaluate_quality(good, modality="vision", model_version="3", baseline=0.95, min_pairs=10)
    assert rbad["breach"] is True and rgood["breach"] is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
