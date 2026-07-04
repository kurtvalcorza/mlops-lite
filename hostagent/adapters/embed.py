"""Embeddings engine adapter (018 US2, T361; 020 T410 — slim child launch).

Off-lease CPU: `POST /engines/embed/embed` relays `{texts}` to the child's `/embed` and returns
the `list[list[float]]` verbatim. All behaviour is the shared `ChildCpuAdapter`; this file only
names the engine + its route + run script.
"""
from hostagent.adapters._child_cpu import ChildCpuAdapter


class EmbedAdapter(ChildCpuAdapter):
    engine_id = "embed"
    verb = "embed"                 # POST /engines/embed/embed -> the child's /embed
    run_script = "embed_run.sh"
