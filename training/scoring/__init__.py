"""In-process score-at-registration (015) — every fine-tune is born with its eval metric.

`score_and_log(name, version, modality, predict_fn)` is the thin seam each fine-tune flow calls
**inside its existing GPU-lease hold, after registering the new version, before releasing the lease**.
It loads the modality's held-out benchmark, runs the per-modality `predict_fn` (the in-memory model
for vision/embeddings, the served GGUF/ggml via a transient llama.cpp/whisper.cpp for LLM/ASR), computes
011's pure-Python primary metric, and logs the `EvalResult` against the new registry version via 011's
`_log_eval` (version tags the gate reads back + a run metric).

Design contract (the grilled decisions, see specs/015-on-demand-version-loading/):
  - **In-process, never a serving daemon** (D1/FR-138) — so the native trainer makes no Docker-only
    hostname call (closes finding #4).
  - **One model in VRAM at any instant** (D7/FR-140) — the caller holds the lease and has already freed
    the training model before a served-artifact scorer (LLM/ASR) loads; vision/embeddings score the
    in-memory trained model that *is* the served artifact.
  - **No new dependency** (FR-145) — the metric math is 011's pure-Python functions; the scorers reuse
    torch / sentence-transformers (already loaded) and the already-built llama.cpp / whisper.cpp binaries.

The per-modality `predict_fn(rows, modality, version) -> list[prediction]` factories live alongside in
`training.scoring.{vision,embeddings,llm,asr}`; this module owns only the load→predict→metric→log glue,
so it is unit-testable offline with an injected `predict_fn` (no GPU, no model).
"""
def _load_evaluation():
    """Import 011's eval harness (metric registry, benchmark loader, `_log_eval`) via the audited
    platformlib bridge (018 T362.1, FR-176 — replaces a per-seam gateway/ path injection)."""
    from platformlib.gateway_bridge import evaluation
    return evaluation()


def _refs_for(modality: str, rows: list) -> list:
    """The per-modality reference list the primary metric scores predictions against.

    LLM/vision/ASR read a single reference field per row (answer / label / text), exactly like 011's
    `evaluate()`. Embeddings' recall@k is self-contained: each query's relevant document is its **own**
    `positive` within the corpus of all positives, so the reference for query *i* is the index *i* (the
    embeddings `predict_fn` returns, per query, a ranked list of corpus indices)."""
    if modality == "embedding":
        return list(range(len(rows)))
    return [r.get("answer", r.get("label", r.get("text"))) for r in rows]


def score_and_log(name, version, modality, predict_fn, *, benchmark=None, client=None,
                  log: bool = True) -> dict:
    """Score `name@version` in-process on its modality's held-out benchmark and (when `log`) record the
    primary metric against the version in MLflow with benchmark provenance — 011's `EvalResult` schema,
    so the gate / compare / quality / HPO read it back unchanged.

    Assumes the **caller holds the GPU lease** and (for LLM/ASR) has already freed the training model, so
    the served-artifact scorer loads with one model in VRAM (Principle II). Returns the result dict; the
    caller logs/warns on it. Raises `evaluation.EvalError` on a benchmark/metric failure (the flow's
    scoring-failure policy decides whether that fails the run or registers-and-warns — see FR + spec).
    """
    ev = _load_evaluation()
    metric = ev.metric_for(modality)
    bench = ev.load_benchmark(modality, benchmark)
    preds = predict_fn(bench.rows, modality, str(version))
    refs = _refs_for(modality, bench.rows)
    value = round(float(metric.score(preds, refs)), 6)

    result = {
        "name": name, "version": str(version), "modality": modality,
        "metric": metric.name, "value": value, "direction": metric.direction,
        "benchmark": bench.name, "benchmark_hash": bench.digest, "n": len(bench.rows),
    }
    if log:
        c = client or ev._client()
        ev._log_eval(c, name, version, result)
    return result


def score_at_registration(name, version, modality, predict_fn, *, log_fn=print, client=None,
                          benchmark=None):
    """Flow-facing wrapper around `score_and_log` with the **scoring-failure policy** (D7 edge / T290):
    a fine-tune whose *training* succeeded but whose *scoring* fails must NOT fail the whole run — the
    version still registers, scoring just **warns** and returns None, and the promotion gate's
    missing-metric policy (PR #20) then applies. Catches broadly on purpose: scoring can fail many
    unrelated ways (a transient llama-server crash, a missing built binary, an OOM on the served
    artifact) and none of those should discard a successfully-trained, registered model.
    """
    try:
        res = score_and_log(name, version, modality, predict_fn, benchmark=benchmark, client=client)
        log_fn(f"scored {name}@{version} at registration: {res['metric']}={res['value']} "
               f"({res['direction']}-better, benchmark={res['benchmark']}/{res['benchmark_hash']})")
        return res
    except Exception as e:  # noqa: BLE001 — register-and-warn is the policy (spec Edge Case / FR-137)
        log_fn(f"WARNING: score-at-registration failed for {name}@{version} ({modality}): {e} — "
               f"version registered WITHOUT an eval metric; the promotion gate's missing-metric policy "
               f"applies")
        return None
