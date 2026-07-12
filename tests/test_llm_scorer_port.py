"""015 regression — the transient LLM scorer port must not collide with the platform's ports.

On-hardware finding: `training/scoring/llm.py` defaulted the transient llama-server scorer to 8099, which
is the **supervisor status server**'s port (`supervisor/supervise.py` SUPERVISE_STATUS_PORT). The scorer's
`POST /completion` then hit the status server's GET-only handler and got 501, so LLM score-at-registration
silently register-and-warned (no `eval_*` tag). Offline suites couldn't catch it — no supervisor runs in
CI. This locks the default off every reserved port so the collision can't regress.
"""
import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ports the platform binds on the reference box (gateway, serving child, the 6 supervised daemons, the
# supervisor status server). The transient scorer must pick something OUTSIDE this set.
RESERVED_PORTS = {
    8080,  # gateway
    8081,  # serving llama-server child
    8082,  # asr whisper-server child (WHISPER_PORT)
    8090,  # serving supervisor
    8091,  # trainer
    8092,  # vision (bento)
    8093,  # embeddings (bento)
    8094,  # tabular (bento)
    8095,  # asr (whisper.cpp supervisor)
    8099,  # supervisor status server (SUPERVISE_STATUS_PORT) — the original collision
}


def _load_llm_scorer():
    # Re-import in isolation so the module-level SCORER_PORT is re-evaluated against the current env.
    spec = importlib.util.spec_from_file_location(
        "llm_scorer_under_test", os.path.join(REPO, "training", "scoring", "llm.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scorer_port_default_does_not_collide_with_reserved_ports():
    os.environ.pop("LLM_SCORER_PORT", None)  # read the shipped default
    m = _load_llm_scorer()
    assert m.SCORER_PORT not in RESERVED_PORTS, (
        f"LLM scorer default port {m.SCORER_PORT} collides with a reserved platform port "
        f"({sorted(RESERVED_PORTS)}) — the scorer's POST would hit another service (e.g. the supervisor "
        f"status server on 8099 → 501)")


def test_scorer_port_is_env_overridable():
    # An operator can still relocate it if their box uses the default elsewhere.
    os.environ["LLM_SCORER_PORT"] = "8123"
    try:
        m = _load_llm_scorer()
        assert m.SCORER_PORT == 8123
    finally:
        os.environ.pop("LLM_SCORER_PORT", None)


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
