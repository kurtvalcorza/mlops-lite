"""Embeddings engine adapter (018 US2, T361) — BentoML CPU service, folded in from serving/bento.

Off-lease CPU: `POST /engines/embed/embed` relays `{texts}` to the child's `/embed` and returns the
`list[list[float]]` verbatim. All behaviour is the shared `BentoCpuAdapter`; this file only names the
engine + its route + run script.
"""
from hostagent.adapters._bento_cpu import BentoCpuAdapter


class EmbedAdapter(BentoCpuAdapter):
    engine_id = "embed"
    verb = "embed"                 # POST /engines/embed/embed -> the child's /embed
    run_script = "embed_run.sh"
    _pip = "sentence-transformers"
