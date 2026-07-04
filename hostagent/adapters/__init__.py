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
from hostagent.adapters.embed import EmbedAdapter
from hostagent.adapters.llama import LlamaAdapter
from hostagent.adapters.tabular import TabularAdapter
from hostagent.adapters.vision import VisionAdapter
from hostagent.adapters.whisper import WhisperAdapter
from platformlib.topology import ENGINES

#: engine_id -> factory(admission) -> adapter. One row per folded-in engine (SC-114). `admission`
#: is the single in-process GPU authority — adapters read it for the holder/free-VRAM fields in
#: their /health (T364 flipped this from the retired lockfile module); adapters that don't need it
#: ignore the argument. The `asr` engine is opt-in (optional=True): it registers here always but
#: reports unavailable until whisper.cpp is built, so platform-health (which treats asr as optional)
#: never stalls on it. embed/tabular are CPU (gpu=False): off-lease, so the lifecycle admits nothing
#: for them (T361).
ADAPTERS = {
    "llm": lambda admission: LlamaAdapter(admission=admission),
    "asr": lambda admission: WhisperAdapter(admission=admission),
    "vision": lambda admission: VisionAdapter(admission=admission),
    "embed": lambda admission: EmbedAdapter(admission=admission),
    "tabular": lambda admission: TabularAdapter(admission=admission),
}


#: Fallback cold-start budget for an engine with no per-engine `ready_wait_s` in topology.ENGINES.
DEFAULT_READY_WAIT_S = 60.0


def _env_float(name):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else None


def build_runtimes(admission) -> dict:
    """One `EngineRuntime` per registered adapter, all under the single admission slot. `kind` is
    "serving" for every folded-in engine (a preemptable GPU/CPU tenant); job runtimes (kind="job",
    never preempted) arrive with the jobs fold-in (T362).

    Per-engine startup + idle budgets (019/US8, FR-196): the retired supervisors gave the slow
    BentoML engines a larger cold-start grace and tuned idle per engine. `ready_wait_s` comes from
    `{ENGINE}_READY_WAIT_S` → `topology.ENGINES[eid]["ready_wait_s"]` → DEFAULT_READY_WAIT_S, so a
    slow first load (BentoML import + weight download) is not killed at a uniform 60s and re-downloaded
    on every retry. Idle honors `{ENGINE}_IDLE_TIMEOUT_S` → the global `IDLE_TIMEOUT` (Codex round 7):
    a fold-in that only flips the gateway URL must not reset a tuned deployment's per-engine idle."""
    global_idle_s = float(os.getenv("IDLE_TIMEOUT", "120"))
    runtimes = {}
    for engine_id, factory in ADAPTERS.items():
        adapter = factory(admission)
        # The topology table is the metadata source of truth; assert the adapter agrees so a
        # gpu/optional drift between the two is caught at wiring time, not in production.
        meta = ENGINES.get(engine_id, {})
        if meta and meta.get("gpu") != adapter.gpu:
            raise ValueError(
                f"adapter {engine_id!r} gpu={adapter.gpu} disagrees with topology.ENGINES "
                f"gpu={meta.get('gpu')}")
        ready_wait_s = (_env_float(f"{engine_id.upper()}_READY_WAIT_S")
                        or float(meta.get("ready_wait_s", DEFAULT_READY_WAIT_S)))
        # CPU engines (gpu=False) are never idle-reaped (T361): the idle reaper exists to free the
        # GPU lease/VRAM, and a CPU engine holds neither — reaping it would only cost a bento
        # respawn (+ model reload), reverting the "always-on" behaviour of the retired daemons.
        if adapter.gpu:
            idle = _env_float(f"{engine_id.upper()}_IDLE_TIMEOUT_S")
            idle = idle if idle is not None else global_idle_s
        else:
            idle = float("inf")
        runtimes[engine_id] = lifecycle.EngineRuntime(adapter, admission, kind="serving",
                                                      idle_timeout_s=idle,
                                                      ready_wait_s=ready_wait_s)
    return runtimes
