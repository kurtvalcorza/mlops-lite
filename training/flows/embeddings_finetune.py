"""Embeddings fine-tune flow (010 US2, T187/T188 — FR-090/FR-091).

Fine-tunes a `sentence-transformers` model with **contrastive / triplet** training on a pinned
pairs/triplets dataset, then logs it via MLflow's `sentence_transformers` flavor + registers a version
tagged `task=embedding` (matching 009's seed/registry vocabulary) so the 009 embeddings service loads it
straight from `models:/{name}@serving` — the exact load contract `serving/bento/embed_service.py` uses.

Default loss is **MultipleNegativesRankingLoss** (in-batch negatives) for pairs and **TripletLoss** when
rows carry a `negative` — a conservative VRAM-fitting default over a few epochs, **exposed on the Runs
form** (configurable, HPO-tunable by 012), not hard-pinned (FR-098). The fine-tune itself is a full GPU
lease tenant (weights + grads + optimizer) held by the trainer daemon; embeddings *serving* stays CPU /
off-lease (009 US2), so only training touches VRAM.

Dataset shape: JSONL rows `{"anchor","positive"}` (pairs) or `{"anchor","positive","negative"}`
(triplets) — the flow validates the shape and picks the loss from it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _common/lineage import as module or script
from _common import MLFLOW_URI, _log, fetch_jsonl, flow, free_cuda  # noqa: E402
from lineage import lineage_tags, link_parent_run, resolve_parent  # noqa: E402

TASK = "embedding"  # singular — matches scripts/seed_embedding_model.py + gateway registry TASK_TAG
DEFAULT_BASE = os.getenv("EMBED_BASE_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


def _parse_rows(rows: list):
    """Validate the embeddings dataset shape → (examples, mode). `mode` is 'triplet' if every row has a
    `negative`, else 'pairs'. Errors clearly if rows aren't anchor/positive pairs (shape mismatch)."""
    anchors, positives, negatives = [], [], []
    has_neg = True
    for i, r in enumerate(rows):
        a = r.get("anchor") or r.get("text1") or r.get("query")
        p = r.get("positive") or r.get("text2") or r.get("passage")
        if not a or not p:
            raise ValueError(f"embeddings row {i} missing anchor/positive — expected pairs/triplets")
        n = r.get("negative")
        anchors.append(a)
        positives.append(p)
        negatives.append(n)
        has_neg = has_neg and bool(n)
    if not anchors:
        raise ValueError("embeddings dataset has no usable {anchor,positive} rows")
    mode = "triplet" if has_neg else "pairs"
    return anchors, positives, negatives, mode


def _cosine_spread(model, anchors, positives) -> float:
    """A small, recorded eval signal: mean cosine(anchor, its positive) minus mean cosine(anchor, a
    shuffled positive). Higher = the model separates true pairs from mismatched ones (a cheap retrieval
    proxy; 013 owns proper eval)."""
    import numpy as np

    a = model.encode(anchors, convert_to_numpy=True, normalize_embeddings=True)
    p = model.encode(positives, convert_to_numpy=True, normalize_embeddings=True)
    pos = float((a * p).sum(axis=1).mean())
    if len(positives) > 1:
        shuffled = np.roll(p, 1, axis=0)
        neg = float((a * shuffled).sum(axis=1).mean())
    else:
        neg = 0.0
    return pos - neg


def _train(anchors, positives, negatives, mode, *, base_model, parent_model_uri, epochs,
           batch_size, warmup_ratio, seed):
    import torch
    from sentence_transformers import InputExample, SentenceTransformer, losses
    from torch.utils.data import DataLoader

    torch.manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    if parent_model_uri:  # chaining (US4): warm-start from a prior registered ST model
        import mlflow.sentence_transformers as st_flavor
        model = st_flavor.load_model(parent_model_uri)
        try:
            model = model.to(device)
        except Exception as e:  # surface the fallback in the run log instead of swallowing (Claude review F4)
            _log(f"warm-start .to({device}) failed, continuing on the loaded device: {e}")
        _log(f"warm-started embeddings from {parent_model_uri}")
    else:
        model = SentenceTransformer(base_model, device=device)
    _log(f"embeddings fine-tune on {device}: {len(anchors)} examples, mode={mode}, base={base_model}")

    if mode == "triplet":
        examples = [InputExample(texts=[a, p, n]) for a, p, n in zip(anchors, positives, negatives)]
        loss = losses.TripletLoss(model=model)
    else:
        examples = [InputExample(texts=[a, p]) for a, p in zip(anchors, positives)]
        loss = losses.MultipleNegativesRankingLoss(model=model)  # in-batch negatives

    loader = DataLoader(examples, shuffle=True, batch_size=batch_size)
    steps_per_epoch = max(len(loader), 1)
    warmup = int(steps_per_epoch * epochs * warmup_ratio)
    model.fit(train_objectives=[(loader, loss)], epochs=epochs, warmup_steps=warmup,
              show_progress_bar=False)

    spread = _cosine_spread(model, anchors, positives)
    metrics = {"cosine_spread": spread, "epochs": epochs, "examples": len(examples), "device": device}
    _log(f"embeddings training done: cosine_spread={spread:.4f}")
    return model, metrics, device


def _register(model, output_name, run_id, *, base_model, dataset_name, dataset_version,
              parent_version, parent_run_id, device):
    """Log the fine-tuned model via the MLflow `sentence_transformers` flavor (so the 009 embed service
    loads `models:/{name}@serving`) and tag the new version for embedding serving + lineage (FR-091)."""
    import mlflow.sentence_transformers as st_flavor
    from mlflow.tracking import MlflowClient

    info = st_flavor.log_model(model, name="model", registered_model_name=output_name)
    c = MlflowClient(tracking_uri=MLFLOW_URI)
    # The flavor populates `registered_model_version` when registered_model_name is given (MLflow ≥2.x;
    # we're on 3.14) — use it directly (Claude review F3): no search/sort, and no race on a newer version
    # landing between log_model and the lookup. Fall back to the newest version by a single-quote-escaped
    # name filter only if it's ever unset.
    version = getattr(info, "registered_model_version", None)
    if version is None:
        safe_name = str(output_name).replace("'", "''")
        versions = sorted(c.search_model_versions(f"name='{safe_name}'"),
                          key=lambda mv: int(mv.version), reverse=True)
        version = versions[0].version
    version = str(version)
    tags = {"kind": "embedding", "framework": "sentence-transformers", "task": TASK,
            "serving_engine": "bentoml", "model_id": base_model, "device": device,
            **lineage_tags(base_model, dataset_name, dataset_version, parent_version, parent_run_id)}
    for k, v in tags.items():
        c.set_model_version_tag(output_name, version, k, str(v))
    _log(f"registered {output_name} v{version} <- run {run_id} (task={TASK}, flavor={info.model_uri}, "
         f"NOT auto-promoted — promotion stays manual)")
    return {"name": output_name, "version": version, "source": f"models:/{output_name}/{version}"}


@flow(name="embeddings-finetune")
def embeddings_finetune_flow(dataset_name: str, dataset_version: str, output_name: str,
                             base_model: str | None = DEFAULT_BASE, epochs: int = 1, batch_size: int = 16,
                             warmup_ratio: float = 0.1, seed: int = 0,
                             parent_version: str | None = None) -> dict:
    """End-to-end embeddings fine-tune → registered, servable `task=embedding` version.

    `parent_version` chains from a prior registered version of `output_name` (US4): it warm-starts from
    that ST model; a parent whose `task` isn't embedding is rejected before training (`resolve_parent`).
    """
    import mlflow

    base = base_model or DEFAULT_BASE
    parent = resolve_parent(output_name, parent_version, TASK) if parent_version else None
    parent_uri = f"models:/{output_name}/{parent['version']}" if parent else None

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("embeddings-finetune")
    with mlflow.start_run() as run:
        run_id = run.info.run_id
        params = dict(modality="embeddings", base_model=base, dataset_name=dataset_name,
                      dataset_version=dataset_version, output_name=output_name, epochs=epochs,
                      batch_size=batch_size, warmup_ratio=warmup_ratio, seed=seed,
                      parent_version=parent_version)
        mlflow.log_params(params)
        link_parent_run(parent["run_id"] if parent else None)

        rows = fetch_jsonl(dataset_name, dataset_version)
        anchors, positives, negatives, mode = _parse_rows(rows)
        try:
            model, metrics, device = _train(
                anchors, positives, negatives, mode, base_model=base, parent_model_uri=parent_uri,
                epochs=epochs, batch_size=batch_size, warmup_ratio=warmup_ratio, seed=seed)
            mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
            mlflow.set_tag("device", device)
            mlflow.set_tag("loss_mode", mode)
            mv = _register(model, output_name, run_id, base_model=base, dataset_name=dataset_name,
                           dataset_version=dataset_version, parent_version=parent_version,
                           parent_run_id=parent["run_id"] if parent else None, device=device)
        finally:
            free_cuda()  # release VRAM whether training succeeded or failed (Principle II)
        mlflow.set_tag("registered_version", mv["version"])
        return {"run_id": run_id, "model": mv, "metrics": metrics, "params": params}


if __name__ == "__main__":
    import json
    a = sys.argv
    print(json.dumps(embeddings_finetune_flow(
        dataset_name=a[1], dataset_version=a[2], output_name=a[3],
        epochs=int(a[4]) if len(a) > 4 else 1), indent=2))
