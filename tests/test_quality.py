"""013 US1/US2 — prediction logging, label join, windowed quality (T242/T246 / SC-075/076/077).

Offline coverage of the data-capture + scoring core: fire-and-forget/fail-open logging, label
ingestion by id (unknown/duplicate/late), the prediction↔label join, and the windowed per-modality
metric (reusing 011's libs) incl. the insufficient-data guard. Live legs (the real endpoints over a
keyed stack) are exercised in test_ui/integration; here the logic is pinned without a store.
"""
import json
import sys

from _quality import install_fakes, load_quality

q = load_quality()


# --- windowed metric (pure) -----------------------------------------------------------------------

def test_windowed_accuracy_over_labeled_pairs_matches_hand_value():
    pairs = ([{"prediction": "cat", "label": "cat", "ts": i} for i in range(18)]
             + [{"prediction": "dog", "label": "cat", "ts": 18 + i} for i in range(2)])  # 18/20
    name, value, direction, n = q.score_window(pairs, "vision", window_n=100, min_pairs=10)
    assert name == "accuracy" and direction == "higher"
    assert value == 0.9 and n == 20  # SC-077: matches the hand-computed value


def test_window_is_last_n_only():
    # 30 pairs: first 10 all wrong, last 20 all right; a window of 20 must score only the recent 20.
    pairs = ([{"prediction": "x", "label": "y", "ts": i} for i in range(10)]
             + [{"prediction": "y", "label": "y", "ts": 10 + i} for i in range(20)])
    _, value, _, n = q.score_window(pairs, "vision", window_n=20, min_pairs=10)
    assert value == 1.0 and n == 20  # the 10 stale wrong ones fell out of the window


def test_thin_window_is_insufficient_data_not_a_number():
    pairs = [{"prediction": "cat", "label": "cat", "ts": i} for i in range(5)]
    _, value, _, n = q.score_window(pairs, "vision", window_n=100, min_pairs=20)
    assert value is None and n == 5  # SC-077: insufficient, never a misleading score


def test_asr_uses_wer_lower_is_better():
    pairs = [{"prediction": "the cat sat", "label": "the cat sat"} for _ in range(25)]
    name, value, direction, _ = q.score_window(pairs, "asr", window_n=100, min_pairs=10)
    assert name == "wer" and direction == "lower" and value == 0.0


def test_unknown_modality_rejected():
    try:
        q.score_window([{"prediction": 1, "label": 1}] * 30, "nonsense")
    except q.QualityError:
        pass
    else:
        raise AssertionError("expected QualityError for an unknown modality")


# --- evaluate_quality verdict (pure) --------------------------------------------------------------

def test_evaluate_quality_reports_breach_relative_to_baseline():
    pairs = ([{"prediction": "cat", "label": "cat", "ts": i} for i in range(16)]
             + [{"prediction": "x", "label": "cat", "ts": 16 + i} for i in range(4)])  # 0.8 accuracy
    # baseline 0.95, 10% drop → threshold 0.855; 0.8 < 0.855 → breach.
    r = q.evaluate_quality(pairs, modality="vision", model_version="3", baseline=0.95, min_pairs=10)
    assert r["value"] == 0.8 and r["breach"] is True and r["insufficient_data"] is False
    # within tolerance → no breach.
    r2 = q.evaluate_quality(pairs, modality="vision", model_version="3", baseline=0.85, min_pairs=10)
    assert r2["breach"] is False


def test_insufficient_window_never_breaches():
    pairs = [{"prediction": "x", "label": "cat", "ts": i} for i in range(3)]  # all wrong but too few
    r = q.evaluate_quality(pairs, modality="vision", model_version="3", baseline=0.95, min_pairs=20)
    assert r["insufficient_data"] is True and r["breach"] is False  # SC-077: no false breach


# --- prediction logging: fire-and-forget + fail-open ----------------------------------------------

def test_log_prediction_returns_id_and_is_fail_open(monkeypatch):
    # even if the store raises on write, logging must NOT raise and must still return a usable id.
    def boom():
        raise RuntimeError("results bucket down")
    monkeypatch.setattr(q, "_s3", boom)
    pid = q.log_prediction("m", "3", "text-generation", "prompt", "out")
    assert isinstance(pid, str) and len(pid) > 0  # SC-075: serving path gets an id regardless


def test_capture_toggle_omits_bodies(monkeypatch):
    fake = install_fakes(q)
    monkeypatch.setattr(q, "QUALITY_CAPTURE_IO", False)
    pid = "fixedid"
    # write synchronously through the same record shape the logger uses.
    q._put_json(f"{q.PRED_PREFIX}{pid}.json", {
        "prediction_id": pid, "model_version": "3", "modality": "text-generation",
        "input_ref": None, "prediction": None})
    rec = json.loads(fake.objs[f"{q.PRED_PREFIX}{pid}.json"])
    assert rec["input_ref"] is None and rec["prediction"] is None


# --- label ingestion + join (fake store) ----------------------------------------------------------

# A real model_name for the seeded rows (NOT None): the store filters `WHERE model_name = %s`, which
# never matches a NULL param against real Postgres — so the `_load_pairs` queries below pass MODEL too.
MODEL = "m"


def _seed_prediction(fake, q, pid, version, modality, pred, ts):
    """Seed a served prediction the way `log_prediction` does under US4: the OUTPUT body in the object
    store + the index row in the relational store."""
    from datetime import datetime, timezone
    ref = f"{q.PRED_PREFIX}{pid}.json"
    fake.objs[ref] = json.dumps({
        "prediction_id": pid, "model_version": version, "modality": modality,
        "prediction": pred, "ts": ts}).encode()
    q._store.log_prediction(q._conn(), pid, MODEL, str(version), modality,
                            datetime.fromtimestamp(ts, timezone.utc),
                            streamed=(pred is None), payload_ref=ref)


def test_attach_label_unknown_duplicate_and_attached():
    fake = install_fakes(q)
    _seed_prediction(fake, q, "p1", "3", "vision", "cat", 1.0)
    assert q.attach_label("missing", "cat")["status"] == "unknown"
    assert q.attach_label("p1", "cat")["status"] == "attached"
    assert q.attach_label("p1", "dog")["status"] == "duplicate"  # write-once, no overwrite
    # US4: the label is relational (write-once PK), not an object — the duplicate did NOT overwrite it.
    assert q._store.labels["p1"]["label"] == "cat"


def test_join_excludes_unlabeled_and_late_label_counts():
    fake = install_fakes(q)
    # three predictions for v3; only two get labels (one is a *late* label after a newer prediction).
    _seed_prediction(fake, q, "a", "3", "vision", "cat", 1.0)
    _seed_prediction(fake, q, "b", "3", "vision", "dog", 2.0)   # stays unlabeled (pending)
    _seed_prediction(fake, q, "c", "3", "vision", "cat", 3.0)
    q.attach_label("c", "cat")            # newer prediction labeled first
    q.attach_label("a", "bird")           # late label for the older prediction
    pairs = q._load_pairs("3", "vision", model_name=MODEL)
    assert len(pairs) == 2                # unlabeled "b" excluded (pending, never scored — SC-076)
    assert [p["ts"] for p in pairs] == [1.0, 3.0]   # chronological; the late label still joined


def test_load_pairs_filters_by_model_version():
    fake = install_fakes(q)
    _seed_prediction(fake, q, "a", "3", "vision", "cat", 1.0)
    _seed_prediction(fake, q, "b", "4", "vision", "cat", 2.0)  # different version
    q.attach_label("a", "cat")
    q.attach_label("b", "cat")
    assert len(q._load_pairs("3", "vision", model_name=MODEL)) == 1  # v4's pair excluded (FR-121)


def test_load_pairs_filters_by_modality_and_model_name():
    # LLM v1 and vision v1 coexist (versions are per-model) — a vision check must not score LLM rows.
    fake = install_fakes(q)
    _seed_prediction(fake, q, "v", "1", "image-classification", "cat", 1.0)
    _seed_prediction(fake, q, "t", "1", "text-generation", "hello", 2.0)
    q.attach_label("v", "cat")
    q.attach_label("t", "hello")
    vis = q._load_pairs("1", "image-classification", model_name=MODEL)
    assert len(vis) == 1 and vis[0]["prediction"] == "cat"   # only the vision row
    llm = q._load_pairs("1", "text-generation", model_name=MODEL)
    assert len(llm) == 1 and llm[0]["prediction"] == "hello"  # only the LLM row


def test_load_pairs_excludes_uncaptured_predictions():
    # streamed / capture-off records log prediction=None — unscorable, must be excluded (not scored "none").
    fake = install_fakes(q)
    _seed_prediction(fake, q, "ok", "3", "text-generation", "answer", 1.0)
    _seed_prediction(fake, q, "stream", "3", "text-generation", None, 2.0)  # uncaptured output
    q.attach_label("ok", "answer")
    q.attach_label("stream", "answer")
    pairs = q._load_pairs("3", "text-generation", model_name=MODEL)
    assert len(pairs) == 1 and pairs[0]["prediction"] == "answer"  # the None-prediction row excluded


def test_attach_label_transient_error_raises_store_error_not_unknown(monkeypatch):
    # a store error while checking the id must surface as QualityStoreError (→502, retryable), not
    # "unknown" — the relational-store equivalent of the old non-404 read error.
    install_fakes(q)

    def boom(conn, pid):
        raise RuntimeError("gateway DB connection reset")   # transient, not a clean "absent"
    monkeypatch.setattr(q._store, "prediction_exists", boom)
    try:
        q.attach_label("p1", "cat")
    except q.QualityStoreError:         # subclass of QualityError → the router maps it to 502
        pass
    else:
        raise AssertionError("expected QualityStoreError on a transient store error")


def test_store_outage_is_store_error_bad_input_is_plain_error(monkeypatch):
    # compute_quality over an unreachable store → QualityStoreError (→502); an unknown modality is a
    # plain QualityError (→400). The router keys the status code off this distinction.
    install_fakes(q)

    def boom(*a, **k):
        raise q._store.StoreError("gateway DB down")
    monkeypatch.setattr(q._store, "window", boom)   # the scoring window read fails
    try:
        q.compute_quality("m", "3", "image-classification", store=False)
    except q.QualityStoreError:
        pass
    else:
        raise AssertionError("expected QualityStoreError on a store outage")
    assert issubclass(q.QualityStoreError, q.QualityError)  # 502-mapped is still a QualityError


def test_store_error_invalidates_conn_so_a_blip_self_heals(monkeypatch):
    # A broken connection must be dropped on the failing op so the NEXT call reconnects — a transient DB
    # blip self-heals instead of every read failing loud (502) until a manual reset_store_conn().
    install_fakes(q)
    assert q._conn() is not None                       # a live cached connection
    n = {"calls": 0}

    def flaky_window(*a, **k):
        n["calls"] += 1
        raise q._store.StoreError("connection reset by peer")
    monkeypatch.setattr(q._store, "window", flaky_window)
    try:
        q._load_pairs("3", "vision", model_name=MODEL)
    except q.QualityStoreError:
        pass
    assert q._conn_state["conn"] is None                # invalidated: the stale connection was dropped
    monkeypatch.undo()                                  # the "DB" is back
    assert q._conn() is not None and n["calls"] == 1    # next call reconnects (recovery), no manual reset


def test_invalidate_conn_only_drops_the_failing_connection():
    # Identity-scoped self-heal: a delayed failure on an ALREADY-superseded connection must NOT close the
    # healthy reconnection another thread established (else the self-heal closes a good connection mid-use).
    install_fakes(q)
    fresh = q._conn()                          # the current healthy cached connection
    stale = object()                           # a different, already-replaced connection object
    q._invalidate_conn(stale)                  # a late failure still referencing the stale conn
    assert q._conn_state["conn"] is fresh and fresh.closed is False  # the fresh one is left intact
    q._invalidate_conn(fresh)                  # the ACTUAL current connection fails
    assert q._conn_state["conn"] is None and fresh.closed is True    # now it is dropped + closed


def test_list_keys_paginates_past_one_page():
    # _list_keys must follow IsTruncated/NextContinuationToken, not stop at the first page.
    class PagedS3:
        def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None, **kw):
            if ContinuationToken is None:
                return {"Contents": [{"Key": "predictions/a.json"}], "IsTruncated": True,
                        "NextContinuationToken": "tok2"}
            return {"Contents": [{"Key": "predictions/b.json"}], "IsTruncated": False}
    keys = q._list_keys(PagedS3(), "predictions/")
    assert keys == ["predictions/a.json", "predictions/b.json"]  # both pages collected


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
