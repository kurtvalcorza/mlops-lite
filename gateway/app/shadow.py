"""Shadow-replay champion-challenger (016) — production-traffic evaluation, advisory only.

011's `compare()` judges champion vs challenger on a fixed held-out benchmark; 016 judges them on the
**real served traffic** the champion actually saw. It reuses 013's logged predictions + delayed labels
(the champion side — **no re-run**, FR-149) and 015's in-process scorers (the challenger side, loaded
sequentially under the single GPU lease — FR-148), over the **captured ∩ labeled** replay window. The
verdict is **advisory** — it never touches the 011/015 promotion gate (FR-150 / SC-097).

This module owns the gateway-side orchestration: resolve the replay window from the store, guard sparse/
absent data, compute the like-for-like verdict, and persist it. The pure pieces (`join_window`,
`build_verdict`) unit-test with injected records; the trainer-side challenger scoring lives in
`training/flows/shadow_replay.py` and is invoked through an injectable seam so the orchestration tests
need no GPU.

US4 (T374): the replay window resolves through the relational `capture_index⋈predictions⋈labels`
join (`quality._store.replay_window`) — one bounded, TTL-filtered index scan — instead of listing
every `inputs/<modality>/` object and reading three objects per pid. Only the recoverable input body
and the champion's logged output are still fetched from MinIO (on the ≤`window_n` joined rows).
FR-176: quality is now imported package-relative (the trainer reaches these cores through
`platformlib.gateway_bridge`, never by loading shadow.py standalone).
"""
import time
from datetime import datetime, timezone

from . import quality


class ShadowError(Exception):
    """A shadow-replay could not be prepared/run (bad modality, store unreachable)."""


# --- pure: window join + verdict ------------------------------------------------------------------

def join_window(input_recs, predictions, labels, *, name, version, modality, window_n):
    """Build the **captured ∩ labeled ∩ champion-scorable** replay window — pure, unit-testable.

    `input_recs`: `[{prediction_id, input, ts}]` (recoverable captured inputs for the modality).
    `predictions`: `{pid: {model_name, model_version, modality, prediction, ts}}` (013 champion log).
    `labels`: `{pid: label_value}` (013 delayed labels).

    A pair joins only when the captured input has a label AND the champion (the `@serving` version)
    actually served it with a **logged, non-None** prediction — so a streamed request (prediction=None)
    or another version's row is excluded (champion-unscorable). Returns the newest `window_n` pairs,
    oldest→newest: `[{prediction_id, input, label, champion_prediction, ts}]`.
    """
    pairs = []
    for rec in input_recs:
        pid = rec.get("prediction_id")
        pred = predictions.get(pid)
        if not pred or pred.get("prediction") is None:
            continue  # never logged a champion prediction (streamed / capture-off) → unscorable
        if str(pred.get("model_version")) != str(version):
            continue  # served by a different version → not the champion's window
        if name and pred.get("model_name") != name:
            continue
        if quality.normalize_modality(pred.get("modality")) != quality.normalize_modality(modality):
            continue  # like-for-like: don't mix modalities that share a version string
        if pid not in labels:
            continue  # unlabeled → pending, excluded
        pairs.append({"prediction_id": pid, "input": rec.get("input"), "label": labels[pid],
                      "champion_prediction": pred.get("prediction"),
                      "options": rec.get("options"),  # served decoding settings, replayed for fidelity
                      "ts": rec.get("ts", pred.get("ts", 0))})
    pairs.sort(key=lambda p: p["ts"])
    return pairs[-window_n:] if window_n and window_n > 0 else pairs


def _score_side(preds, refs, modality):
    """Score one side's predictions against the labels with 011's modality metric — the single metric
    definition both sides share (like-for-like, FR-150)."""
    metric = quality._modality_metric(modality)
    return round(float(metric.score(preds, refs)), 6), metric.name, metric.direction


def build_verdict(name, champion_version, challenger_version, modality, pairs, challenger_preds):
    """Pure advisory verdict: champion (logged predictions) vs challenger (replayed predictions) over the
    **same** `(input, label)` window, honouring the modality metric + direction. `challenger_preds` is
    the challenger's prediction per pair (same order as `pairs`). Always `advisory: True` (never gates)."""
    if len(challenger_preds) != len(pairs):
        raise ShadowError(
            f"challenger produced {len(challenger_preds)} predictions for {len(pairs)} replay pairs")
    refs = [p["label"] for p in pairs]
    champ_val, metric_name, direction = _score_side([p["champion_prediction"] for p in pairs], refs,
                                                    modality)
    chall_val, _, _ = _score_side(list(challenger_preds), refs, modality)
    higher = quality._eval().HIGHER
    if champ_val == chall_val:
        winner = "tie"
    elif (chall_val > champ_val) == (direction == higher):
        winner = "challenger"
    else:
        winner = "champion"
    return {
        "status": "completed", "name": name, "modality": quality.normalize_modality(modality),
        "metric": metric_name, "direction": direction, "n_pairs": len(pairs),
        "champion": {"version": str(champion_version), "value": champ_val},
        "challenger": {"version": str(challenger_version), "value": chall_val},
        "winner": winner, "advisory": True,
    }


# --- I/O: resolve the replay window from the store ------------------------------------------------

def resolve_window(name, version, modality, *, window_n=None, now=None, ttl_s=None):
    """Join the captured inputs ↔ champion predictions ↔ labels into the replay window for the
    champion `name@version` + `modality`. US4: the join is the relational
    `capture_index⋈predictions⋈labels` `store.replay_window` (a bounded, indexed, TTL-filtered scan
    of the newest `window_n` rows) — not a listing of every `inputs/<modality>/` object. Only the
    recoverable input body (`input_ref`) and the champion's logged output (`payload_ref`) are then
    GET from MinIO, on those ≤`window_n` rows.

    Two robustness properties beyond the pure join:
      - **TTL filter at resolve time**, not just on capture-write: if traffic for a modality stops,
        no later `capture_input` runs the prune, so a delayed label could otherwise pull an expired
        input into a ready window. The cutoff is applied IN THE QUERY (`captured_at >= cutoff`), no
        key parse. `now`/`ttl_s` are injectable for tests; `ttl_s<=0` disables (as `inputs_to_prune`).
      - **Store-read failures surface** (ShadowError) instead of silently dropping a row: a failed
        window query aborts, and for the per-row body GETs only a *confirmed-missing* object
        (404/NoSuchKey — a prune race) is skipped; a transient MinIO error aborts the replay rather
        than compute an advisory verdict on a partial, biased window.
    """
    window_n = quality.WINDOW_N if window_n is None else window_n
    modality = quality.normalize_modality(modality)
    now = time.time() if now is None else now
    ttl_s = quality.SHADOW_CAPTURE_TTL_S if ttl_s is None else ttl_s
    ttl_cutoff = (datetime.fromtimestamp(now - ttl_s, timezone.utc)
                  if ttl_s and ttl_s > 0 else None)
    conn = quality._conn()
    if conn is None:
        raise ShadowError("shadow-replay store unreachable — cannot resolve the replay window")
    try:
        rows = quality._store.replay_window(conn, modality, name, str(version), window_n,
                                            ttl_cutoff=ttl_cutoff)
    except Exception as e:
        quality._invalidate_conn()  # a broken connection self-heals on the next replay
        raise ShadowError(f"shadow-replay window query failed: {e}") from e
    input_recs, predictions, labels = [], {}, {}
    for r in rows:
        pid = r["prediction_id"]
        try:
            rec = quality._get_json(r["input_ref"])  # the recoverable served input body
        except Exception as e:
            if quality._missing(e):
                continue  # input body pruned between the index row and the get → skip
            raise ShadowError(f"shadow-replay store read failed for input {pid}: {e}") from e
        input_recs.append(rec)
        labels[pid] = r["label"]  # the label is relational now (no per-pid object GET)
        payload_ref = r.get("payload_ref")
        if not payload_ref:
            continue  # streamed / no output body → unscorable, excluded by join_window
        try:
            predictions[pid] = quality._get_json(payload_ref)  # the champion's logged output body
        except Exception as e:
            if quality._missing(e):
                continue  # champion output pruned → excluded by join_window
            raise ShadowError(f"shadow-replay store read failed for prediction {pid}: {e}") from e
    return join_window(input_recs, predictions, labels, name=name, version=version, modality=modality,
                       window_n=window_n)


def has_captured_inputs(modality) -> bool:
    """True if ANY recoverable input is captured for `modality` (else shadow-replay has no corpus).
    US4: an indexed `capture_index` existence check, not a listing; a store outage fails loud."""
    modality = quality.normalize_modality(modality)
    conn = quality._conn()
    if conn is None:
        raise ShadowError("shadow-replay store unreachable — cannot check the capture corpus")
    try:
        return quality._store.has_captures(conn, modality)
    except Exception as e:
        quality._invalidate_conn()  # reconnect on the next check rather than propagate a stale-conn error
        raise ShadowError(f"shadow-replay corpus check failed: {e}") from e


# --- guards: decide whether a replay can run (US3) ------------------------------------------------

def prepare(name, version, modality, *, window_n=None, min_pairs=None) -> dict:
    """Resolve the replay window and apply the US3 guards (FR-152). Returns either a **ready** result
    (`{"status": "ready", "pairs": [...]}`) or a terminal non-verdict status the endpoint surfaces:

      - `no_corpus` — capture is disabled (`QUALITY_CAPTURE_IO` off) → no replay inputs at all.
      - `inputs_not_captured` — capture on but nothing captured for this modality yet.
      - `insufficient_data` — fewer than `min_pairs` captured∩labeled∩scorable pairs.
    """
    min_pairs = quality.MIN_PAIRS if min_pairs is None else min_pairs
    modality = quality.normalize_modality(modality)
    if modality not in quality.SHADOW_MODALITIES:
        raise ShadowError(f"shadow-replay is not supported for modality {modality!r} "
                          f"(replayable: {sorted(quality.SHADOW_MODALITIES)})")
    if not quality.QUALITY_CAPTURE_IO:
        return {"status": "no_corpus",
                "detail": "QUALITY_CAPTURE_IO is off — no replay inputs captured"}
    if not has_captured_inputs(modality):
        return {"status": "inputs_not_captured", "detail": f"inputs not captured for {modality}"}
    pairs = resolve_window(name, version, modality, window_n=window_n)
    if len(pairs) < min_pairs:
        return {"status": "insufficient_data", "n_pairs": len(pairs), "min": min_pairs}
    return {"status": "ready", "pairs": pairs, "n_pairs": len(pairs)}


# --- persist / read the advisory verdict (results `shadow/` prefix) -------------------------------

def persist_verdict(shadow_id: str, verdict: dict) -> None:
    quality._put_json(f"{quality.SHADOW_PREFIX}{shadow_id}.json", {**verdict, "shadow_id": shadow_id})


def read_verdict(shadow_id: str):
    try:
        return quality._get_json(f"{quality.SHADOW_PREFIX}{shadow_id}.json")
    except Exception:
        return None


# --- orchestration: prepare → score the challenger → verdict → persist ----------------------------

def run_replay(name, champion_version, challenger_version, modality, *, shadow_id, scorer,
               window_n=None, min_pairs=None) -> dict:
    """Full advisory shadow-replay: resolve+guard the window, run the challenger `scorer` over it, build
    the verdict, and persist it under `shadow/<id>`. Runs **on the trainer** (the `scorer` loads the
    challenger artifact under the GPU lease — one model in VRAM); the gateway dispatches it.

    `scorer(pairs, modality, challenger_version) -> [challenger_pred]` is the trainer-side seam
    (`training.flows.shadow_replay.score_challenger` on-hardware; injected in tests). A non-ready guard
    status is persisted + returned verbatim (no scoring). The champion is **not** re-run (FR-149)."""
    prep = prepare(name, champion_version, modality, window_n=window_n, min_pairs=min_pairs)
    if prep["status"] != "ready":
        result = {**{k: v for k, v in prep.items() if k != "pairs"}, "shadow_id": shadow_id, "name": name}
        persist_verdict(shadow_id, result)
        return result
    pairs = prep["pairs"]
    challenger_preds = scorer(pairs, modality, str(challenger_version))
    verdict = build_verdict(name, champion_version, challenger_version, modality, pairs, challenger_preds)
    verdict = {**verdict, "shadow_id": shadow_id, "name": name,
               "challenger": {**verdict["challenger"]}}
    persist_verdict(shadow_id, verdict)
    return verdict
