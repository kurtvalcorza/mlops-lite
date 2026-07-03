"""Engine adapter registry (018 US2, FR-170 / SC-114).

Adding a serving engine is ONE adapter module + ONE row here — zero edits to admission, lifecycle,
swap, or the agent's HTTP surface (the consolidation claim `tests/test_agent_adapters.py` pins).
`build_runtimes` wires each registered adapter into the shared lifecycle; the engine's gpu/optional
metadata is the single `platformlib.topology.ENGINES` table, so the adapter and the registry can
never disagree.

Fold-in order (each flips one gateway URL + deletes one legacy daemon in its phase):
    T358 llm → T359 asr → T360 vision → T361 embed/tabular. Jobs (kind="job") arrive at T362.
"""
import os

from hostagent import lifecycle
from hostagent.adapters.llama import LlamaAdapter
from hostagent.adapters.whisper import WhisperAdapter
from platformlib.topology import ENGINES

#: engine_id -> factory(lease) -> adapter. One row per folded-in engine (SC-114). `lease` is the
#: legacy lockfile module (migration interop); adapters that don't need it ignore the argument. The
#: `asr` engine is opt-in (optional=True): it registers here always but reports unavailable until
#: whisper.cpp is built, so platform-health (which treats asr as optional) never stalls on it.
ADAPTERS = {
    "llm": lambda lease: LlamaAdapter(lease=lease),
    "asr": lambda lease: WhisperAdapter(lease=lease),
}


def build_runtimes(admission, lease=None) -> dict:
    """One `EngineRuntime` per registered adapter, all under the single admission slot. `kind` is
    "serving" for every folded-in engine (a preemptable GPU/CPU tenant); job runtimes (kind="job",
    never preempted) arrive with the jobs fold-in (T362).

    Honors the legacy `IDLE_TIMEOUT` env (Codex round 7, 018): the retired supervisors read it to
    tune how long a resident model stays before the reaper releases VRAM. A fold-in that only flips
    the gateway URL must not reset a tuned deployment's idle timeout to the default."""
    idle_timeout_s = float(os.getenv("IDLE_TIMEOUT", "120"))
    runtimes = {}
    for engine_id, factory in ADAPTERS.items():
        adapter = factory(lease)
        # The topology table is the metadata source of truth; assert the adapter agrees so a
        # gpu/optional drift between the two is caught at wiring time, not in production.
        meta = ENGINES.get(engine_id, {})
        if meta and meta.get("gpu") != adapter.gpu:
            raise ValueError(
                f"adapter {engine_id!r} gpu={adapter.gpu} disagrees with topology.ENGINES "
                f"gpu={meta.get('gpu')}")
        runtimes[engine_id] = lifecycle.EngineRuntime(adapter, admission, kind="serving",
                                                      idle_timeout_s=idle_timeout_s)
    return runtimes
