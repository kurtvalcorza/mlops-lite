"""In-process embeddings scorer (015) — recall@k from the in-memory sentence-transformers model.

The fine-tuned ST model **is** the served artifact (009 embed service loads the same model), so scoring
runs in-memory (D6), no quantization gap. The held-out fixture is a list of `{query, positive}` pairs;
the scorer encodes every query and the **corpus of all positives**, ranks the corpus per query by cosine
similarity, and returns — per query — the ranked list of corpus indices. Paired with
`score_and_log`'s `_refs_for("embedding", rows) == range(len(rows))`, recall@k counts a hit when a
query's own positive lands in the top-k: a self-contained retrieval metric over a tiny set.
"""


def make_predict_fn(model):
    """Build a `predict_fn(rows, modality, version)` that returns, per query, the corpus indices ranked
    by descending cosine similarity to the query (the corpus = every row's `positive`). Uses the
    in-memory `model.encode` with normalized embeddings, so a dot product *is* cosine similarity — no
    numpy dependency in this glue (the ST model already pulls torch/numpy)."""

    def predict_fn(rows, _modality, _version):
        queries = [r.get("query", r.get("anchor", r.get("text1", ""))) for r in rows]
        corpus = [r.get("positive", r.get("text2", r.get("passage", ""))) for r in rows]
        q = model.encode(queries, convert_to_numpy=True, normalize_embeddings=True)
        d = model.encode(corpus, convert_to_numpy=True, normalize_embeddings=True)
        sims = q @ d.T  # (n_queries, n_docs) cosine similarity (both sides L2-normalized)
        ranked = []
        for row in sims:
            order = sorted(range(len(corpus)), key=lambda j: float(row[j]), reverse=True)
            ranked.append(order)
        return ranked

    return predict_fn
